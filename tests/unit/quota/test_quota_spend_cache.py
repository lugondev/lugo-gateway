"""The pre-flight gate reads a cached spend aggregate -- and it must stay exact.

quota_gate runs before every conversation turn, before every farewell, and on
every metered HTTP route. Each applicable quota cost it one SUM over the month's
usage_events, a scan that grows with the deployment's usage, re-derived several
times a second.

Caching that is only acceptable if a cost this process just wrote is visible to
the very next gate call. record_usage folds each new cost into the cached
aggregate (note_spend); the TTL is only a backstop for what this process cannot
see.
"""

import pytest

from app.services.quota import gate as gate_mod
from app.services.quota.gate import QuotaExceededError, quota_gate
from app.services.quota.store import quota_store
from app.services.usage.recorder import record_usage

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clear_cache():
    gate_mod.invalidate_spend_cache()
    yield
    gate_mod.invalidate_spend_cache()


async def test_the_aggregate_is_not_recomputed_on_every_call(monkeypatch):
    await quota_store.create(scope="global", scope_id="", limit_usd=100.0, period="total")

    calls = {"n": 0}
    real = gate_mod.current_spend

    async def counting(**kwargs):
        calls["n"] += 1
        return await real(**kwargs)

    monkeypatch.setattr(gate_mod, "current_spend", counting)

    for _ in range(5):
        await quota_gate(user_id="u1", provider_id="", kind="llm")

    assert calls["n"] == 1


async def test_a_cost_just_written_is_visible_to_the_next_check():
    """The whole point: a cached number that lags is a budget that doesn't hold."""
    await quota_store.create(scope="user", scope_id="u2", limit_usd=1.0, period="total")

    # Warms the cache at $0 -- under the limit, so this passes.
    await quota_gate(user_id="u2", provider_id="", kind="llm")

    # Write a row whose cost blows the limit. record_usage prices it from the
    # registry (nothing configured here, so $0), which is why the cost is folded
    # in directly -- the assertion is about note_spend, not about pricing.
    await record_usage(
        user_id="u2", profile_id="", kind="llm", engine="e", model_id="m",
        unit="tokens", native_amount=10,
    )
    gate_mod.note_spend(user_id="u2", provider_id="", cost_usd=2.0)

    with pytest.raises(QuotaExceededError):
        await quota_gate(user_id="u2", provider_id="", kind="llm")


async def test_a_cost_for_someone_else_does_not_move_a_user_quota():
    await quota_store.create(scope="user", scope_id="u3", limit_usd=1.0, period="total")
    await quota_gate(user_id="u3", provider_id="", kind="llm")

    gate_mod.note_spend(user_id="somebody-else", provider_id="", cost_usd=99.0)

    await quota_gate(user_id="u3", provider_id="", kind="llm")  # still under


async def test_any_cost_moves_a_global_quota():
    await quota_store.create(scope="global", scope_id="", limit_usd=1.0, period="total")
    await quota_gate(user_id="u4", provider_id="", kind="llm")

    gate_mod.note_spend(user_id="anyone-at-all", provider_id="", cost_usd=5.0)

    with pytest.raises(QuotaExceededError):
        await quota_gate(user_id="u4", provider_id="", kind="llm")
