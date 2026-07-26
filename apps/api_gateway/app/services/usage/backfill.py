"""One-time, idempotent backfill of usage_events rows whose model_id is "".

Rows written before usage/attribution.py existed recorded model_id="" whenever
the caller didn't know the model (a /synthesize with no model_id, a session
whose profile pinned no LLM, ...). Those rows read as "(none)" in the Usage
dashboards and, more importantly, can never match the registry row carrying
the price.

Only PROVABLE rows are rewritten: the engine must have exactly one
non-sentinel registry model. Two candidates means either answer could be wrong,
so the row keeps its blank and the UI labels it honestly. cost_usd is never
touched -- recomputing history from today's prices would fabricate billing.

Safe on every boot: once rewritten, a row no longer matches the WHERE clause.
"""

from __future__ import annotations

import logging

from sqlalchemy import distinct, select, update

from app.services.db.engine import db_session
from app.services.db.models import UsageEvent
from app.services.model_registry.store import model_registry_store

logger = logging.getLogger(__name__)


async def migrate_backfill_usage_model_ids() -> int:
    """Number of rows updated. Never raises -- a failed backfill must not stop
    the app from booting."""
    updated = 0
    try:
        async with db_session() as s:
            groups = (
                await s.execute(
                    select(distinct(UsageEvent.kind), UsageEvent.engine)
                    .where(UsageEvent.model_id == "", UsageEvent.engine != "")
                )
            ).all()
        if not groups:
            return 0

        entries = await model_registry_store.list_all()
        for kind, engine in groups:
            candidates = [
                e for e in entries
                if e["kind"] == kind and e["engine"] == engine and e["model_id"]
            ]
            enabled = [c for c in candidates if c["enabled"]]
            pool = enabled or candidates
            if len(pool) != 1:
                logger.info(
                    "usage backfill: leaving %s/%s blank (%d candidate models)",
                    kind, engine, len(pool),
                )
                continue
            model_id = pool[0]["model_id"]
            async with db_session() as s:
                result = await s.execute(
                    update(UsageEvent)
                    .where(
                        UsageEvent.kind == kind,
                        UsageEvent.engine == engine,
                        UsageEvent.model_id == "",
                    )
                    .values(model_id=model_id)
                )
                await s.commit()
            count = result.rowcount or 0
            updated += count
            if count:
                logger.info(
                    "usage backfill: %s/%s -> model_id=%s (%d rows)",
                    kind, engine, model_id, count,
                )
    except Exception as exc:  # noqa: BLE001 - a backfill must never block boot
        logger.warning("usage model_id backfill failed: %s", exc)
    return updated
