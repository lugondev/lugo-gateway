import httpx
import pytest
from sqlalchemy import select

from app.services.db.engine import db_session, init_db
from app.services.db.models import UsageEvent
from app.services.memory.retriever import MemoryRetriever
from app.services.memory.store import memory_store
from app.services.profiles.models import Profile


async def _usage_rows(kind=None):
    async with db_session() as s:
        rows = (await s.execute(select(UsageEvent))).scalars().all()
    return [r for r in rows if kind is None or r.kind == kind]


@pytest.fixture
def _fake_embeddings(monkeypatch):
    async def fake_post(self, url, headers=None, json=None):
        class R:
            def raise_for_status(self):
                pass

            def json(self):
                n = len(json.get("input") or [])
                return {
                    "data": [{"embedding": [1.0, 0.0]} for _ in range(n)],
                    "usage": {"prompt_tokens": 7 * n},
                }

        return R()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


@pytest.mark.asyncio
async def test_embed_texts_with_usage_returns_prompt_tokens(_fake_embeddings):
    from app.services.memory.embedder import embed_texts, embed_texts_with_usage

    vecs, tokens = await embed_texts_with_usage(["a", "b"], "http://llm.local/v1", "k", "emb")
    assert len(vecs) == 2 and tokens == 14
    # The old signature still works for callers that don't meter.
    assert len(await embed_texts(["a"], "http://llm.local/v1", "k", "emb")) == 1


@pytest.mark.asyncio
async def test_missing_usage_block_counts_as_zero_tokens(monkeypatch):
    async def fake_post(self, url, headers=None, json=None):
        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"data": [{"embedding": [0.5]}]}  # no "usage" key at all

        return R()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    from app.services.memory.embedder import embed_texts_with_usage

    vecs, tokens = await embed_texts_with_usage(["a"], "http://llm.local/v1", "k", "emb")
    assert len(vecs) == 1 and tokens == 0


@pytest.mark.asyncio
async def test_semantic_retrieval_records_embed_usage(_fake_embeddings):
    await init_db()
    await memory_store.add("metering", "user likes tea", embedding=[1.0, 0.0], user_id="u9")
    profile = Profile(
        name="metering",
        llm={"base_url": "http://llm.local/v1", "model": "m", "engine": "openai"},
        memory={"mode": "semantic", "embed_model": "text-embedding-3-small"},
    )
    await MemoryRetriever().get_context(profile, query="trà", user_id="u9")

    rows = await _usage_rows("embed")
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id == "u9"
    assert row.profile_id == "metering"
    assert row.engine == "openai"
    assert row.model_id == "text-embedding-3-small"
    assert row.unit == "tokens"
    assert row.native_amount == 7 and row.prompt_tokens == 7


@pytest.mark.asyncio
async def test_retrieval_still_works_when_metering_blows_up(monkeypatch, _fake_embeddings):
    await init_db()
    await memory_store.add("metering2", "fact", embedding=[1.0, 0.0])

    async def boom(**kwargs):
        raise RuntimeError("recorder down")

    monkeypatch.setattr("app.services.memory.retriever.record_usage", boom)
    profile = Profile(
        name="metering2",
        llm={"base_url": "http://llm.local/v1", "model": "m", "engine": "openai"},
        memory={"mode": "semantic", "embed_model": "e"},
    )
    # Must NOT raise, and must still return the memory block.
    assert "fact" in await MemoryRetriever().get_context(profile, query="q")


@pytest.mark.asyncio
async def test_extractor_meters_its_llm_call_and_its_embedding(monkeypatch):
    await init_db()
    from app.services.history.store import session_store
    from app.services.memory.extractor import MemoryExtractor

    await session_store.create("s-meter", profile_id="ex")
    await session_store.append_message("s-meter", 1, "user", "tôi thích trà")
    await session_store.append_message("s-meter", 1, "assistant", "ok")

    async def fake_post(self, url, headers=None, json=None):
        class R:
            def raise_for_status(self):
                pass

            def json(self):
                if url.endswith("/embeddings"):
                    n = len(json.get("input") or [])
                    return {
                        "data": [{"embedding": [0.1]} for _ in range(n)],
                        "usage": {"prompt_tokens": 5 * n},
                    }
                return {
                    "choices": [{"message": {"content": '["User likes tea"]'}}],
                    "usage": {"prompt_tokens": 120, "completion_tokens": 8},
                }

        return R()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    profile = Profile(
        name="ex",
        llm={"base_url": "http://llm.local/v1", "model": "m", "engine": "openai"},
        memory={"embed_model": "text-embedding-3-small"},
    )
    added = await MemoryExtractor().extract_and_upsert("s-meter", profile, user_id="u7")
    assert added == 1

    llm_rows = [r for r in await _usage_rows("llm") if r.profile_id == "ex"]
    assert len(llm_rows) == 1
    assert llm_rows[0].prompt_tokens == 120 and llm_rows[0].completion_tokens == 8
    assert llm_rows[0].native_amount == 128
    assert llm_rows[0].user_id == "u7" and llm_rows[0].engine == "openai"
    assert llm_rows[0].model_id == "m"

    embed_rows = [r for r in await _usage_rows("embed") if r.profile_id == "ex"]
    assert len(embed_rows) == 1
    assert embed_rows[0].prompt_tokens == 5
    assert embed_rows[0].model_id == "text-embedding-3-small"


@pytest.mark.asyncio
async def test_extractor_uses_the_extractor_model_id_when_set(monkeypatch):
    await init_db()
    from app.services.history.store import session_store
    from app.services.memory.extractor import MemoryExtractor

    await session_store.create("s-model", profile_id="exm")
    await session_store.append_message("s-model", 1, "user", "hi there")
    await session_store.append_message("s-model", 1, "assistant", "hello")

    async def fake_post(self, url, headers=None, json=None):
        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "choices": [{"message": {"content": '["User says hi"]'}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                }

        return R()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    profile = Profile(
        name="exm",
        llm={"base_url": "http://llm.local/v1", "model": "chat-model", "engine": "openai"},
        memory={"extractor_model": "cheap-model"},
    )
    await MemoryExtractor().extract_and_upsert("s-model", profile, user_id="u8")
    rows = [r for r in await _usage_rows("llm") if r.profile_id == "exm"]
    # Attribution must name the model that was actually billed, not the chat one.
    assert [r.model_id for r in rows] == ["cheap-model"]


@pytest.mark.asyncio
async def test_compactor_meters_its_llm_call(monkeypatch):
    await init_db()
    from app.services.memory.compactor import MemoryCompactor

    for i in range(3):
        await memory_store.add("cmp", f"fact {i}", user_id="u5")

    async def fake_post(self, url, headers=None, json=None):
        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "choices": [{"message": {"content": "## User Profile\n### Danh tính\n- x"}}],
                    "usage": {"prompt_tokens": 300, "completion_tokens": 40},
                }

        return R()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    profile = Profile(
        name="cmp",
        llm={"base_url": "http://llm.local/v1", "model": "chat", "engine": "openai"},
        memory={"extractor_model": "cheap", "compaction_threshold": 2},
    )
    assert await MemoryCompactor().maybe_compact(profile, user_id="u5") is True

    rows = [r for r in await _usage_rows("llm") if r.profile_id == "cmp"]
    assert len(rows) == 1
    assert rows[0].model_id == "cheap"          # the model actually billed
    assert rows[0].engine == "openai"
    assert rows[0].user_id == "u5"
    assert rows[0].prompt_tokens == 300 and rows[0].completion_tokens == 40
    assert rows[0].native_amount == 340
