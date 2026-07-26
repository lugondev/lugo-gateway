import logging

from fastapi import APIRouter, HTTPException, Request

from app.core.actor import current_user_id
from app.services.usage.query import summarize, summarize_for_user

router = APIRouter(prefix="/v1/usage", tags=["usage"])
logger = logging.getLogger(__name__)


async def _attach_labels(group_by: str, rows: list[dict]) -> None:
    """Add a human-readable `label` to id-keyed rows, in place.

    `summarize` groups by the raw column, so provider and user rows come back
    keyed by uuid -- unreadable in a dashboard. Only those two dimensions get a
    label; kind/engine/model keys are already names, and labelling them would
    just duplicate the key.

    A key with no match gets `label = ""` rather than a guess. That covers the
    blank provider_id (a local engine, not linked to any provider) and the blank
    user_id (the shared-device bucket) -- both are meaningful states, not
    missing data, and the client words them accordingly.
    """
    if group_by == "provider":
        from app.services.providers.store import provider_store

        names = {p["id"]: (p["label"] or p["name"]) for p in await provider_store.list_all()}
    elif group_by == "user":
        from app.services.auth.users import user_store

        names = {u["id"]: u["username"] for u in await user_store.list()}
    else:
        return
    for row in rows:
        row["label"] = names.get(row["key"], "")


@router.get("/summary")
async def get_summary(group_by: str, period: str | None = None) -> dict:
    """Admin-only cross-tenant rollup, sliced by one of
    user|provider|model|kind|engine. See auth_guard._ADMIN_PREFIXES."""
    try:
        data = await summarize(group_by, period)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _attach_labels(group_by, data)
    return {"success": True, "data": data}


async def _limits_for(user_id: str) -> list[dict]:
    """The quotas that can block THIS caller: their own user quota and the
    global one. Never another user's, and never a provider quota -- its spend is
    cross-tenant information, and this endpoint is open to every logged-in user.
    """
    from app.services.quota.gate import current_spend
    from app.services.quota.store import quota_store

    out: list[dict] = []
    try:
        quotas = await quota_store.list_enabled()
    except Exception as exc:  # noqa: BLE001 - never break the usage view over this
        logger.warning("reading own limits failed: %s", exc)
        return out
    for quota in quotas:
        if quota["scope"] != "global" and not (
            quota["scope"] == "user" and quota["scope_id"] == user_id
        ):
            continue
        # Per row, not per list (same guard as GET /v1/quotas): one unreadable
        # spend must cost that row its number, not drop it and every row after
        # it. The client presents this as the complete set of limits that can
        # block the caller, so a silently truncated list reads as "you have no
        # global quota" -- a wrong answer where 0.0 is only a stale one.
        try:
            spend = await current_spend(
                scope=quota["scope"], scope_id=quota["scope_id"], period=quota["period"]
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("reading spend for a %s limit failed: %s", quota["scope"], exc)
            spend = 0.0
        out.append({
            "scope": quota["scope"],
            "period": quota["period"],
            "limit_usd": quota["limit_usd"],
            "spend_usd": spend,
        })
    return out


@router.get("/me")
async def get_my_usage(request: Request, period: str | None = None) -> dict:
    """The caller's own usage, grouped by (kind, model_id). Carved out of the
    admin-only /v1/usage prefix for any logged-in user -- see
    auth_guard._USER_PREFIXES."""
    user_id = current_user_id(request) or ""
    data = await summarize_for_user(user_id, period)
    return {"success": True, "data": data, "limits": await _limits_for(user_id)}
