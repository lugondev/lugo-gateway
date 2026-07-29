"""The design spec (§7) requires a blocked request to leave an audit row.
Without it, a quota block is invisible after the fact: the request never
appears in usage (it did no work) and nothing else records that it happened."""

from sqlalchemy import select

from app.services.db.engine import db_session, init_db
from app.services.db.models import UsageEvent
from app.services.model_registry.store import model_registry_store
from app.services.quota.gate import QuotaExceededError, current_spend, quota_gate
from app.services.quota.store import quota_store
from app.services.usage.recorder import record_usage


async def _rows(status=None):
    async with db_session() as s:
        rows = (await s.execute(select(UsageEvent))).scalars().all()
    return [r for r in rows if status is None or r.status == status]


async def _spend_over_a_one_dollar_user_quota(user_id: str) -> None:
    """Put `user_id` over a $1 monthly quota with one priced usage row."""
    await init_db()
    quota_store.invalidate()
    await model_registry_store.create(
        "llm", "OA", "priced-model", "Priced",
        config={"provider_id": "prov-oa", "price": {"unit": "1M_tokens", "in": 10.0, "out": 0.0}},
    )
    await record_usage(user_id=user_id, profile_id="p", kind="llm", engine="OA",
                       model_id="priced-model", unit="tokens", native_amount=1_000_000,
                       prompt_tokens=1_000_000)
    await quota_store.create(scope="user", scope_id=user_id, limit_usd=1.0, period="monthly")


async def test_blocking_writes_one_audit_row():
    await _spend_over_a_one_dollar_user_quota("u-audit")
    try:
        await quota_gate(user_id="u-audit", provider_id="", kind="stt",
                         engine="qwencloud", model_id="fun-asr", profile_id="pro")
        raise AssertionError("expected the gate to block")
    except QuotaExceededError:
        pass

    blocked = await _rows("blocked")
    assert len(blocked) == 1
    row = blocked[0]
    assert row.kind == "stt" and row.engine == "qwencloud" and row.model_id == "fun-asr"
    assert row.user_id == "u-audit" and row.profile_id == "pro"
    assert row.unit == "seconds"      # the audit row still says what was refused
    assert row.native_amount == 0.0   # nothing was served
    assert row.cost_usd == 0.0


async def test_a_blocked_row_can_never_feed_the_spend_that_caused_it():
    """The dangerous failure: if a blocked row carried cost, each block would
    raise the spend that triggers the next block."""
    await _spend_over_a_one_dollar_user_quota("u-feedback")
    before = await current_spend(scope="user", scope_id="u-feedback", period="monthly")
    for _ in range(3):
        try:
            await quota_gate(user_id="u-feedback", provider_id="", kind="llm",
                             engine="OA", model_id="priced-model")
        except QuotaExceededError:
            pass
    after = await current_spend(scope="user", scope_id="u-feedback", period="monthly")
    assert after == before
    assert len(await _rows("blocked")) == 3


async def test_no_audit_row_when_the_caller_names_no_kind():
    await _spend_over_a_one_dollar_user_quota("u-nokind")
    try:
        await quota_gate(user_id="u-nokind", provider_id="")
    except QuotaExceededError:
        pass
    assert await _rows("blocked") == []


async def test_allowed_requests_write_no_audit_row():
    await init_db()
    quota_store.invalidate()
    await quota_store.create(scope="user", scope_id="u-under", limit_usd=100.0, period="monthly")
    await quota_gate(user_id="u-under", provider_id="", kind="llm", engine="OA", model_id="m")
    assert await _rows("blocked") == []


async def test_a_failing_audit_write_still_blocks(monkeypatch):
    """The block is the point; the audit row is bookkeeping. A recorder failure
    must not turn a refusal into a served request."""
    await _spend_over_a_one_dollar_user_quota("u-recfail")

    async def boom(**kwargs):
        raise RuntimeError("recorder down")

    monkeypatch.setattr("app.services.quota.gate.record_usage", boom)
    try:
        await quota_gate(user_id="u-recfail", provider_id="", kind="llm",
                         engine="OA", model_id="priced-model")
        raise AssertionError("expected the gate to block even though the audit write failed")
    except QuotaExceededError:
        pass


async def test_the_block_is_logged_with_the_quota_that_tripped(caplog):
    import logging

    await _spend_over_a_one_dollar_user_quota("u-log")
    with caplog.at_level(logging.WARNING, logger="app.services.quota.gate"):
        try:
            await quota_gate(user_id="u-log", provider_id="", kind="llm",
                             engine="OA", model_id="priced-model")
        except QuotaExceededError:
            pass
    blocked_logs = [r for r in caplog.records if "quota exceeded" in r.getMessage()]
    assert blocked_logs, "a block must be visible in the logs, not only in the DB"
    assert "u-log" in blocked_logs[0].getMessage()
