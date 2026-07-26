"""Read-only aggregation over `usage_events` (T1's UsageEvent table).

Two entry points:
- `summarize`: admin-facing, grouped by any one of the columns in
  `_GROUP_COLUMNS`.
- `summarize_for_user`: same aggregation, but scoped to one user_id and
  always grouped by (kind, engine, model_id) -- the breakdown a user's own
  "my usage" view needs, regardless of what an admin might slice by.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select

from app.services.db.engine import db_session
from app.services.db.models import UsageEvent

# Maps the API's public group_by names to the UsageEvent column they
# aggregate on. Not a 1:1 name match for two of them (user->user_id,
# provider->provider_id, model->model_id) -- kind/engine already match the
# column name directly.
_GROUP_COLUMNS = {
    "user": UsageEvent.user_id,
    "provider": UsageEvent.provider_id,
    "model": UsageEvent.model_id,
    "kind": UsageEvent.kind,
    "engine": UsageEvent.engine,
}


def _period_range(period_key: str) -> tuple[datetime, datetime]:
    """"YYYY-MM" -> [start, end) half-open UTC range covering that month."""
    year_str, month_str = period_key.split("-", 1)
    year, month = int(year_str), int(month_str)
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc) if month == 12 else datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return start, end


async def summarize(group_by: str, period_key: str | None = None) -> list[dict]:
    """SUM(cost_usd)/SUM(native_amount)/COUNT(*) grouped by one of
    user|provider|model|kind|engine, optionally restricted to one "YYYY-MM"
    month of `ts`. Raises ValueError on an unknown group_by."""
    column = _GROUP_COLUMNS.get(group_by)
    if column is None:
        raise ValueError(
            f"unknown group_by '{group_by}' (valid: {', '.join(sorted(_GROUP_COLUMNS))})"
        )

    stmt = select(
        column.label("key"),
        func.sum(UsageEvent.cost_usd).label("cost_usd"),
        func.sum(UsageEvent.native_amount).label("native_amount"),
        func.count().label("count"),
    ).group_by(column)
    if period_key:
        start, end = _period_range(period_key)
        stmt = stmt.where(UsageEvent.ts >= start, UsageEvent.ts < end)

    async with db_session() as s:
        rows = (await s.execute(stmt)).all()
    return [
        {
            "key": row.key,
            "cost_usd": float(row.cost_usd or 0.0),
            "native_amount": float(row.native_amount or 0.0),
            "count": int(row.count),
        }
        for row in rows
    ]


async def summarize_for_user(user_id: str, period_key: str | None = None) -> list[dict]:
    """Same aggregation as `summarize`, scoped to one user_id and grouped by
    (kind, engine, model_id) -- the breakdown behind a user's own "my usage"
    view. Engine is part of the key because a row whose model couldn't be
    attributed (see usage/attribution.py) is still identifiable by its engine."""
    stmt = select(
        UsageEvent.kind.label("kind"),
        UsageEvent.engine.label("engine"),
        UsageEvent.model_id.label("model_id"),
        func.sum(UsageEvent.cost_usd).label("cost_usd"),
        func.sum(UsageEvent.native_amount).label("native_amount"),
        func.count().label("count"),
    ).where(UsageEvent.user_id == user_id).group_by(
        UsageEvent.kind, UsageEvent.engine, UsageEvent.model_id
    )
    if period_key:
        start, end = _period_range(period_key)
        stmt = stmt.where(UsageEvent.ts >= start, UsageEvent.ts < end)

    async with db_session() as s:
        rows = (await s.execute(stmt)).all()
    return [
        {
            "kind": row.kind,
            "engine": row.engine,
            "model_id": row.model_id,
            "cost_usd": float(row.cost_usd or 0.0),
            "native_amount": float(row.native_amount or 0.0),
            "count": int(row.count),
        }
        for row in rows
    ]
