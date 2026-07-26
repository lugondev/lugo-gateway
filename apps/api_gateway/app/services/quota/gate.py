from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.services.db.engine import db_session
from app.services.db.models import UsageEvent
from app.services.quota.store import quota_store
from app.services.usage.query import _period_range
from app.services.usage.recorder import record_usage

logger = logging.getLogger(__name__)

# The unit an audit row carries per kind, matching what the metering call sites
# use for real usage so the two are comparable in a query.
_UNIT_BY_KIND = {"llm": "tokens", "embed": "tokens", "stt": "seconds", "tts": "chars"}


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


async def quota_gate(
    *, user_id: str, provider_id: str,
    kind: str = "", engine: str = "", model_id: str = "", profile_id: str = "",
) -> None:
    """Pre-flight: raise QuotaExceededError if any applicable enabled quota is at/over
    its limit for the current period. FAIL-OPEN: any other error logs and allows.

    kind/engine/model_id/profile_id describe the work being refused. When `kind`
    is given, a block also writes one `status="blocked"` audit row -- otherwise
    a refused request leaves no trace at all, since it does no work and so never
    appears in usage. They are optional so a caller with nothing meaningful to
    say (UsageEvent.kind is NOT NULL) simply gets no audit row.
    """
    try:
        quotas = await quota_store.list_enabled()
        for q in quotas:
            if not _applies(q, user_id, provider_id):
                continue
            spend = await current_spend(scope=q["scope"], scope_id=q["scope_id"], period=q["period"])
            if spend >= q["limit_usd"] > 0:
                raise QuotaExceededError(q["scope"], q["scope_id"], q["limit_usd"], spend, q["period"])
    except QuotaExceededError as exc:
        logger.warning(
            "quota block: user=%r provider=%r kind=%r engine=%r model=%r -- %s",
            user_id, provider_id, kind, engine, model_id, exc,
        )
        await _record_block(user_id, profile_id, kind, engine, model_id)
        raise
    except Exception as exc:  # noqa: BLE001 - fail-open, never deny service on a gate bug
        logger.warning("quota_gate failed open: %s", exc)


async def _record_block(user_id: str, profile_id: str, kind: str, engine: str, model_id: str) -> None:
    """Best-effort audit row for a refused request. Zero amount and zero cost:
    nothing was served, and a blocked row that carried cost would inflate the
    very spend that caused the block."""
    if not kind:
        return
    try:
        await record_usage(
            user_id=user_id or "", profile_id=profile_id or "", kind=kind,
            engine=engine or "", model_id=model_id or "",
            unit=_UNIT_BY_KIND.get(kind, ""), native_amount=0.0, status="blocked",
        )
    except Exception as exc:  # noqa: BLE001 - the block matters, the audit row does not
        logger.warning("quota block audit row failed: %s", exc)
