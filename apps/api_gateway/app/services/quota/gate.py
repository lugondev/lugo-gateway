from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.services.db.engine import db_session
from app.services.db.models import UsageEvent
from app.services.quota.store import quota_store
from app.services.usage.query import _period_range

logger = logging.getLogger(__name__)


class QuotaExceededError(Exception):
    def __init__(self, scope, scope_id, limit_usd, spend_usd, period):
        self.scope, self.scope_id = scope, scope_id
        self.limit_usd, self.spend_usd, self.period = limit_usd, spend_usd, period
        super().__init__(
            f"{scope} quota exceeded"
            + (f" for {scope_id}" if scope_id else "")
            + f": ${spend_usd:.4f} / ${limit_usd:.4f} ({period})"
        )


def _current_month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


async def current_spend(*, scope: str, scope_id: str, period: str) -> float:
    stmt = select(func.coalesce(func.sum(UsageEvent.cost_usd), 0.0))
    if scope == "user":
        stmt = stmt.where(UsageEvent.user_id == scope_id)
    elif scope == "provider":
        stmt = stmt.where(UsageEvent.provider_id == scope_id)
    # global: no scope filter
    if period == "monthly":
        start, end = _period_range(_current_month_key())
        stmt = stmt.where(UsageEvent.ts >= start, UsageEvent.ts < end)
    async with db_session() as s:
        return float((await s.execute(stmt)).scalar_one() or 0.0)


def _applies(q: dict, user_id: str, provider_id: str) -> bool:
    if q["scope"] == "global":
        return True
    if q["scope"] == "user":
        return q["scope_id"] == (user_id or "")
    if q["scope"] == "provider":
        return bool(provider_id) and q["scope_id"] == provider_id
    return False


async def quota_gate(*, user_id: str, provider_id: str) -> None:
    """Pre-flight: raise QuotaExceededError if any applicable enabled quota is at/over
    its limit for the current period. FAIL-OPEN: any other error logs and allows."""
    try:
        quotas = await quota_store.list_enabled()
        for q in quotas:
            if not _applies(q, user_id, provider_id):
                continue
            spend = await current_spend(scope=q["scope"], scope_id=q["scope_id"], period=q["period"])
            if spend >= q["limit_usd"] > 0:
                raise QuotaExceededError(q["scope"], q["scope_id"], q["limit_usd"], spend, q["period"])
    except QuotaExceededError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail-open, never deny service on a gate bug
        logger.warning("quota_gate failed open: %s", exc)
