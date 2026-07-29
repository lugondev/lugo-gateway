"""Task 6 (A1) seam tests for the shared LLM-turn usage recorder
(services/conversation/turn_usage.py), extracted out of
api/routes/livehost.py's _record_llm_usage closure and
services/conversation/session.py's _record_llm_usage method.
tests/unit/conversation/test_session_usage_metering.py exercises
ConversationSession._record_llm_usage end to end (unchanged, still the
pre-extraction contract fence for the session.py call site) -- these tests
drive the shared helper directly, plus the livehost-only `llm_model`
override argument that test_session_usage_metering.py has no reason to
cover."""

import pytest
from sqlalchemy import select

from app.services.db.engine import db_session, init_db
from app.services.db.models import UsageEvent
from app.services.profiles.models import LlmConfig, Profile


class _FakeResponder:
    name = "fake"

    def __init__(self, model: str | None = None, last_usage: dict | None = None):
        if model is not None:
            self.model = model
        self.last_usage = last_usage


async def _rows() -> list[UsageEvent]:
    async with db_session() as s:
        return list((await s.execute(select(UsageEvent))).scalars().all())


@pytest.mark.asyncio
async def test_record_llm_turn_usage_writes_a_row_from_profile_pins():
    from app.services.conversation.turn_usage import record_llm_turn_usage

    await init_db()
    profile = Profile(name="p", llm=LlmConfig(engine="pinned-engine", model="pinned-model"))
    responder = _FakeResponder(last_usage={"prompt_tokens": 4, "completion_tokens": 1})

    await record_llm_turn_usage(
        responder, identity_user_id="u-1", profile=profile, profile_name="p",
    )

    rows = await _rows()
    llm = next(r for r in rows if r.kind == "llm")
    assert (llm.engine, llm.model_id) == ("pinned-engine", "pinned-model")
    assert llm.user_id == "u-1" and llm.profile_id == "p"
    assert llm.prompt_tokens == 4 and llm.completion_tokens == 1


@pytest.mark.asyncio
async def test_record_llm_turn_usage_llm_model_override_wins_like_livehost():
    """livehost.py's closure passes its own resolved `llm_model` (a registry
    override the profile alone wouldn't carry) rather than relying purely on
    profile.llm.model -- same rule as turn_quota's `llm_model` param."""
    from app.services.conversation.turn_usage import record_llm_turn_usage

    await init_db()
    profile = Profile(name="p2", llm=LlmConfig(engine="pinned-engine", model="pinned-model"))
    responder = _FakeResponder(last_usage={"prompt_tokens": 2, "completion_tokens": 2})

    await record_llm_turn_usage(
        responder, identity_user_id="u-2", profile=profile, profile_name="p2",
        llm_model="override-model",
    )

    rows = await _rows()
    llm = next(r for r in rows if r.kind == "llm" and r.profile_id == "p2")
    # override-model != responder's model_id source (responder has no `.model`
    # attr here) and == pinned_model passed to resolve_llm_pair, so engine
    # stays "pinned-engine" (model_id == pinned_model) per resolve_llm_pair's rule.
    assert llm.model_id == "override-model"
    assert llm.engine == "pinned-engine"


@pytest.mark.asyncio
async def test_record_llm_turn_usage_never_raises_on_bad_input():
    """Metering must never break a turn: no responder.last_usage, no profile."""
    from app.services.conversation.turn_usage import record_llm_turn_usage

    await init_db()
    responder = _FakeResponder()
    await record_llm_turn_usage(
        responder, identity_user_id=None, profile=None, profile_name=None,
    )
    # No exception raised is the assertion; a row may or may not exist
    # depending on resolve_usage_model's default-pair fallback, which isn't
    # this test's concern.
