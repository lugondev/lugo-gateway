"""livehost had no quota gate at all: an over-limit user could simply use this
endpoint instead of the gated ones."""

import pytest

from app.services.db.engine import init_db
from app.services.model_registry.store import model_registry_store
from app.services.quota.store import quota_store
from app.services.usage.recorder import record_usage


@pytest.mark.asyncio
async def test_livehost_module_gates_its_turns():
    """A structural check: both turn entry points must reach quota_gate.

    livehost's turn functions are closures over a live WebSocket, so driving
    them end to end needs the full socket harness. This asserts the wiring
    exists; the gate's own behavior is covered by tests/unit/quota/test_quota_gate.py
    and the REST paths in tests/unit/quota/test_quota_provider_scope.py.
    """
    import inspect

    from app.api.routes import livehost

    source = inspect.getsource(livehost)
    assert "quota_gate" in source, "livehost must gate its turns"
    # Both paths, not just the voice one.
    voice = source.split("async def _run_voice_turn")[1].split("async def run_voice_turn")[0]
    social = source.split("async def _run_social_turn")[1].split("async def run_social_turn")[0]
    assert "_quota_blocked" in voice, "the voice turn must check the quota"
    assert "_quota_blocked" in social, "the social turn must check the quota"


@pytest.mark.asyncio
async def test_livehost_quota_helper_blocks_when_over_limit():
    """The helper both turn paths call, exercised directly."""
    from app.api.routes.livehost import _quota_blocked_for

    await init_db()
    quota_store.invalidate()
    await model_registry_store.create(
        "llm", "OA", "lh-model", "LH",
        config={"provider_id": "prov-lh", "price": {"unit": "1M_tokens", "in": 10.0, "out": 0.0}},
    )
    await record_usage(user_id="u-lh", profile_id="", kind="llm", engine="OA",
                       model_id="lh-model", unit="tokens", native_amount=1_000_000,
                       prompt_tokens=1_000_000)
    await quota_store.create(scope="user", scope_id="u-lh", limit_usd=1.0, period="monthly")

    blocked, message = await _quota_blocked_for(
        user_id="u-lh", profile_name="p", pinned_engine="OA", pinned_model="lh-model",
    )
    assert blocked is True
    assert "quota exceeded" in message

    under, message2 = await _quota_blocked_for(
        user_id="u-nobody", profile_name="p", pinned_engine="OA", pinned_model="lh-model",
    )
    assert under is False and message2 == ""


@pytest.mark.asyncio
async def test_livehost_quota_helper_fails_open():
    from app.api.routes import livehost

    await init_db()
    quota_store.invalidate()

    async def boom(**kwargs):
        raise RuntimeError("quota subsystem down")

    original = livehost.quota_gate
    livehost.quota_gate = boom
    try:
        blocked, message = await livehost._quota_blocked_for(
            user_id="u-x", profile_name="p", pinned_engine="OA", pinned_model="m",
        )
        assert blocked is False and message == ""
    finally:
        livehost.quota_gate = original
