import httpx
import pytest

from app.services.db.engine import init_db
from app.services.history.store import session_store
from app.services.memory.extractor import MemoryExtractor
from app.services.memory.store import memory_store
from app.services.model_registry.store import model_registry_store
from app.services.profiles.models import Profile
from app.services.quota.store import quota_store
from app.services.usage.recorder import record_usage


@pytest.fixture
def _call_spy(monkeypatch):
    """Records every provider URL that would be called, and lets the call
    genuinely succeed (returns a normal one-fact extraction body) if it
    happens. This way, if the gate fails to block, extraction actually
    succeeds and adds a memory -- the spy doesn't rely on exception
    semantics, so no catch-all in extract() can hide a call that shouldn't
    have happened.
    """
    calls: list[str] = []

    async def spy(self, url, headers=None, json=None):
        calls.append(url)

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "choices": [{"message": {"content": '["User likes tea"]'}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 3},
                }

        return R()

    monkeypatch.setattr(httpx.AsyncClient, "post", spy)
    return calls


async def _seed_session(session_id, profile_name):
    await session_store.create(session_id, profile_id=profile_name)
    await session_store.append_message(session_id, 1, "user", "tôi thích trà")
    await session_store.append_message(session_id, 1, "assistant", "ok")


@pytest.mark.asyncio
async def test_over_quota_skips_extraction_entirely(_call_spy):
    await init_db()
    quota_store.invalidate()
    await _seed_session("s-quota", "qp")
    await model_registry_store.create(
        "llm", "openai", "m-quota", "priced",
        config={"provider_id": "prov-q", "price": {"unit": "1M_tokens", "in": 1000.0, "out": 0.0}},
    )
    # 1M tokens at $1000/1M = $1000 of spend against a $1 limit.
    await record_usage(user_id="u-quota", profile_id="qp", kind="llm", engine="openai",
                       model_id="m-quota", unit="tokens", native_amount=1_000_000,
                       prompt_tokens=1_000_000)
    await quota_store.create(scope="user", scope_id="u-quota", limit_usd=1.0, period="monthly")

    profile = Profile(
        name="qp",
        llm={"base_url": "http://llm.local/v1", "model": "m-quota", "engine": "openai"},
    )
    assert await MemoryExtractor().extract_and_upsert("s-quota", profile, user_id="u-quota") == 0
    assert await memory_store.list("qp", user_id="u-quota") == []
    assert _call_spy == [], f"provider was called while over quota: {_call_spy}"


@pytest.mark.asyncio
async def test_under_quota_still_extracts(monkeypatch):
    await init_db()
    quota_store.invalidate()
    await _seed_session("s-under", "up")

    async def fake_post(self, url, headers=None, json=None):
        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "choices": [{"message": {"content": '["User likes tea"]'}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 3},
                }

        return R()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    await quota_store.create(scope="user", scope_id="u-under", limit_usd=100.0, period="monthly")
    profile = Profile(
        name="up",
        llm={"base_url": "http://llm.local/v1", "model": "m", "engine": "openai"},
    )
    assert await MemoryExtractor().extract_and_upsert("s-under", profile, user_id="u-under") == 1


@pytest.mark.asyncio
async def test_gate_failure_fails_open(monkeypatch):
    await init_db()
    quota_store.invalidate()
    await _seed_session("s-open", "op")

    async def gate_boom(**kwargs):
        raise RuntimeError("quota subsystem down")

    monkeypatch.setattr("app.services.quota.gate.quota_gate", gate_boom)

    async def fake_post(self, url, headers=None, json=None):
        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": '["User likes tea"]'}}]}

        return R()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    profile = Profile(
        name="op",
        llm={"base_url": "http://llm.local/v1", "model": "m", "engine": "openai"},
    )
    # A broken gate must not stop memory work.
    assert await MemoryExtractor().extract_and_upsert("s-open", profile, user_id="u-open") == 1
