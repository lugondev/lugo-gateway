from __future__ import annotations

import logging
import uuid

from app.services.db.engine import db_session
from app.services.db.models import UsageEvent
from app.services.model_registry.store import model_registry_store
from app.services.usage.attribution import resolve_usage_model
from app.services.usage.pricing import compute_cost

logger = logging.getLogger(__name__)


async def record_usage(*, user_id, profile_id, kind, engine, model_id, unit,
                       native_amount, prompt_tokens=None, completion_tokens=None,
                       request_id=None, status="ok"):
    """Best-effort append of one usage row. Resolves provider_id + price from the
    Model Registry entry, computes cost. NEVER raises into the caller -- a metering
    failure must not break the STT/TTS/LLM turn that triggered it."""
    try:
        # Blanks get resolved here rather than at each of the 9 call sites: a
        # blank model_id can't match the registry row that carries the price, so
        # it would silently cost $0 forever (and read as "(none)" in the UI).
        engine, model_id = await resolve_usage_model(kind, engine, model_id)
        entry = await model_registry_store.find(kind, engine, model_id)
        cfg = (entry or {}).get("config") or {}
        provider_id = cfg.get("provider_id") or ""
        cost = compute_cost(cfg.get("price"), prompt_tokens, completion_tokens, native_amount)
        async with db_session() as s:
            s.add(UsageEvent(
                id=str(uuid.uuid4()), user_id=user_id or "", profile_id=profile_id or "",
                provider_id=provider_id, kind=kind, engine=engine, model_id=model_id,
                unit=unit, native_amount=float(native_amount or 0.0),
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                cost_usd=cost, request_id=request_id, status=status,
            ))
            await s.commit()
        # Keep the quota pre-flight's cached aggregate exact rather than merely
        # fresh: the gate now reads a cached SUM (it runs before every turn and
        # re-scanning the month each time does not scale), so a cost this
        # process just wrote has to be folded in, not waited out. Imported
        # function-locally -- quota.gate imports this module, so a top-level
        # import would be a cycle.
        from app.services.quota.gate import note_spend

        note_spend(user_id=user_id or "", provider_id=provider_id, cost_usd=cost)
    except Exception as exc:  # noqa: BLE001 - metering must never break a turn
        logger.warning("record_usage failed (%s/%s/%s): %s", kind, engine, model_id, exc)
