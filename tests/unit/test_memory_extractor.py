import pytest

from app.services.db import engine as db_engine
from app.services.memory.extractor import MemoryExtractor, _parse_facts
from app.services.memory.store import memory_store
from app.services.history.store import session_store
from app.services.profiles.models import Profile


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    db_engine.configure(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    yield
    db_engine.configure()


def test_parse_facts_plain_array():
    assert _parse_facts('["a", "b"]') == ["a", "b"]


def test_parse_facts_fenced_and_prose():
    raw = 'Here you go:\n```json\n["User likes tea", "User is a dev"]\n```'
    assert _parse_facts(raw) == ["User likes tea", "User is a dev"]


def test_parse_facts_garbage_returns_empty():
    assert _parse_facts("no json here") == []
    assert _parse_facts('{"not": "an array"}') == []
    assert _parse_facts('[1, 2, {"x": 3}]') == []


def test_parse_facts_trailing_prose_with_brackets():
    raw = 'Here:\n["User likes tea", "User is a dev"]\nSee item[1] above.'
    assert _parse_facts(raw) == ["User likes tea", "User is a dev"]


def test_parse_facts_fact_containing_brackets():
    assert _parse_facts('["User works on [ESP32] firmware"]') == ["User works on [ESP32] firmware"]


@pytest.mark.asyncio
async def test_extract_calls_llm(monkeypatch):
    captured = {}

    async def fake_post(self, url, headers=None, json=None):
        captured["url"] = url
        captured["json"] = json

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": '["User speaks Vietnamese"]'}}]}

        return R()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    ex = MemoryExtractor()
    facts = await ex.extract(
        [{"role": "user", "content": "xin chào"}, {"role": "assistant", "content": "chào"}],
        base_url="http://llm.local/v1", api_key="k", model="m",
    )
    assert facts == ["User speaks Vietnamese"]
    assert captured["url"] == "http://llm.local/v1/chat/completions"
    assert captured["json"]["model"] == "m"


@pytest.mark.asyncio
async def test_extract_and_upsert_dedupes(monkeypatch):
    await session_store.create("s1", profile_id="pet")
    await session_store.append_message("s1", 1, "user", "tôi thích trà")
    await session_store.append_message("s1", 1, "assistant", "ok")
    await memory_store.add("pet", "user likes tea")

    async def fake_extract(self, messages, base_url, api_key, model):
        return ["User Likes Tea", "User is from Hanoi"]

    monkeypatch.setattr(MemoryExtractor, "extract", fake_extract)
    profile = Profile(name="pet", llm={"base_url": "http://llm.local/v1", "model": "m"})
    added = await MemoryExtractor().extract_and_upsert("s1", profile)
    assert added == 1  # tea fact deduped case-insensitively
    contents = {m["content"] for m in await memory_store.list("pet")}
    assert "User is from Hanoi" in contents


@pytest.mark.asyncio
async def test_extract_and_upsert_skips_short_or_no_llm():
    await session_store.create("s2", profile_id="pet")
    await session_store.append_message("s2", 1, "user", "hi")
    profile = Profile(name="pet", llm={"base_url": "http://llm.local/v1", "model": "m"})
    assert await MemoryExtractor().extract_and_upsert("s2", profile) == 0  # <2 messages
    no_llm = Profile(name="pet")
    assert await MemoryExtractor().extract_and_upsert("s1", no_llm) == 0
