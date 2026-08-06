"""Task 6 (A1) seam tests for the shared LLM-turn quota preflight
(services/conversation/turn_quota.py), extracted out of api/routes/livehost.py's
_quota_blocked_for / services/conversation/session.py's _run_turn /
api/routes/conversation.py's chat(). Mirrors
tests/unit/livehost/test_livehost_quota_gate.py's two functional tests, which
remain in place unchanged as the pre-extraction contract fence."""

import pytest

from app.services.db.engine import init_db
from app.services.model_registry.store import model_registry_store
from app.services.profiles.models import LlmConfig, Profile
from app.services.quota.store import quota_store
from app.services.usage.recorder import record_usage


@pytest.mark.asyncio
async def test_llm_turn_quota_blocked_over_limit_via_profile():
    """Profile-based call shape (session.py / conversation.py's style)."""
    from app.services.conversation.turn_quota import llm_turn_quota_blocked

    await init_db()
    quota_store.invalidate()
    await model_registry_store.create(
        "llm", "OA", "tq-model", "TQ",
        config={"provider_id": "prov-tq", "price": {"unit": "1M_tokens", "in": 10.0, "out": 0.0}},
    )
    await record_usage(user_id="u-tq", profile_id="", kind="llm", engine="OA",
                       model_id="tq-model", unit="tokens", native_amount=1_000_000,
                       prompt_tokens=1_000_000)
    await quota_store.create(scope="user", scope_id="u-tq", limit_usd=1.0, period="monthly")

    profile = Profile(name="p", llm=LlmConfig(engine="OA", model="tq-model"))

    blocked, message = await llm_turn_quota_blocked(
        identity_user_id="u-tq", profile=profile, profile_name="p",
    )
    assert blocked is True
    assert "quota exceeded" in message

    under, message2 = await llm_turn_quota_blocked(
        identity_user_id="u-nobody", profile=profile, profile_name="p",
    )
    assert under is False and message2 == ""


@pytest.mark.asyncio
async def test_llm_turn_quota_blocked_fails_open_on_gate_error():
    """quota_gate raising anything other than QuotaExceededError must never
    block the turn -- passed explicitly here (the way api/routes/livehost.py's
    _quota_blocked_for wrapper does, so a monkeypatch of ITS module-level
    `quota_gate` name is honored -- see test_livehost_quota_gate.py's
    test_livehost_quota_helper_fails_open for that exact idiom)."""
    from app.services.conversation.turn_quota import llm_turn_quota_blocked_for_pins

    await init_db()
    quota_store.invalidate()

    async def boom(**kwargs):
        raise RuntimeError("quota subsystem down")

    blocked, message = await llm_turn_quota_blocked_for_pins(
        user_id="u-x", profile_name="p", pinned_engine="OA", pinned_model="m",
        quota_gate=boom,
    )
    assert blocked is False and message == ""


@pytest.mark.asyncio
async def test_llm_turn_quota_blocked_default_quota_gate_is_function_local():
    """No quota_gate passed -> resolves app.services.quota.gate.quota_gate at
    call time, so a monkeypatch of THAT module's attribute (session.py's and
    conversation.py's own idiom) is observed."""
    import app.services.quota.gate as gate_mod
    from app.services.conversation.turn_quota import llm_turn_quota_blocked_for_pins

    await init_db()
    quota_store.invalidate()

    async def boom(**kwargs):
        raise RuntimeError("quota subsystem down")

    original = gate_mod.quota_gate
    gate_mod.quota_gate = boom
    try:
        blocked, message = await llm_turn_quota_blocked_for_pins(
            user_id="u-x", profile_name="p", pinned_engine="OA", pinned_model="m",
        )
        assert blocked is False and message == ""
    finally:
        gate_mod.quota_gate = original


@pytest.mark.asyncio
async def test_an_injected_text_turn_is_quota_gated():
    """The livehost plugin drives social turns with {"type":"text"}. The gate
    sits above _run_turn's audio/text branch, so text is covered by
    construction -- this pins that construction down, because the plugin has
    no gate of its own any more."""
    import inspect

    from app.services.conversation import session as session_module

    source = inspect.getsource(session_module.ConversationSession._run_turn)
    gate_at = source.index("llm_turn_quota_blocked")
    text_branch_at = source.index("if text_input is not None")
    assert gate_at < text_branch_at, (
        "the quota gate must run before _run_turn splits into the audio and "
        "text paths, or an injected social turn skips it"
    )
