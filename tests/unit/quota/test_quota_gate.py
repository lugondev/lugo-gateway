import pytest
from app.services.db.engine import init_db, db_session
from app.services.db.models import UsageEvent
from app.services.quota.store import quota_store
from app.services.quota.gate import quota_gate, QuotaExceededError


async def _add_cost(user_id, provider_id, cost):
    import uuid
    async with db_session() as s:
        s.add(UsageEvent(id=str(uuid.uuid4()), user_id=user_id, profile_id="", provider_id=provider_id,
                         kind="llm", engine="e", model_id="m", unit="tokens", native_amount=1, cost_usd=cost))
        await s.commit()


async def test_blocks_when_user_over_limit():
    await init_db()
    await quota_store.create(scope="user", scope_id="u1", limit_usd=1.0, period="total")
    await _add_cost("u1", "prov", 1.5)  # over
    with pytest.raises(QuotaExceededError):
        await quota_gate(user_id="u1", provider_id="prov")


async def test_allows_under_limit_and_other_user():
    await init_db()
    await quota_store.create(scope="user", scope_id="u1", limit_usd=10.0, period="total")
    await _add_cost("u1", "prov", 2.0)
    await quota_gate(user_id="u1", provider_id="prov")     # under → no raise
    await quota_gate(user_id="u2", provider_id="prov")     # different user, no quota → no raise


async def test_global_and_provider_scopes():
    await init_db()
    await quota_store.create(scope="global", scope_id="", limit_usd=1.0, period="total")
    await _add_cost("uX", "provA", 2.0)
    with pytest.raises(QuotaExceededError):
        await quota_gate(user_id="anyone", provider_id="provA")


async def test_fail_open_on_internal_error(monkeypatch):
    await init_db()
    async def boom(): raise RuntimeError("down")
    monkeypatch.setattr(quota_store, "list_enabled", boom)
    await quota_gate(user_id="u", provider_id="p")   # must NOT raise
