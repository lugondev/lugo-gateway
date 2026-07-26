from fastapi import APIRouter, HTTPException, Request

from app.core.actor import current_user_id
from app.services.usage.query import summarize, summarize_for_user

router = APIRouter(prefix="/v1/usage", tags=["usage"])


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


@router.get("/me")
async def get_my_usage(request: Request, period: str | None = None) -> dict:
    """The caller's own usage, grouped by (kind, model_id). Carved out of the
    admin-only /v1/usage prefix for any logged-in user -- see
    auth_guard._USER_PREFIXES."""
    user_id = current_user_id(request) or ""
    data = await summarize_for_user(user_id, period)
    return {"success": True, "data": data}
