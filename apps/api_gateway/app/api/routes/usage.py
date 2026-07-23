from fastapi import APIRouter, HTTPException, Request

from app.core.actor import current_user_id
from app.services.usage.query import summarize, summarize_for_user

router = APIRouter(prefix="/v1/usage", tags=["usage"])


@router.get("/summary")
async def get_summary(group_by: str, period: str | None = None) -> dict:
    """Admin-only cross-tenant rollup, sliced by one of
    user|provider|model|kind|engine. See auth_guard._ADMIN_PREFIXES."""
    try:
        data = await summarize(group_by, period)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, "data": data}


@router.get("/me")
async def get_my_usage(request: Request, period: str | None = None) -> dict:
    """The caller's own usage, grouped by (kind, model_id). Carved out of the
    admin-only /v1/usage prefix for any logged-in user -- see
    auth_guard._USER_PREFIXES."""
    user_id = current_user_id(request) or ""
    data = await summarize_for_user(user_id, period)
    return {"success": True, "data": data}
