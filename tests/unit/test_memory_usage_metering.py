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
