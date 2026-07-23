from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.quota.store import quota_store

router = APIRouter(prefix="/v1/quotas", tags=["quotas"])

_VALID_SCOPES = {"user", "provider", "global"}
_VALID_PERIODS = {"monthly", "total"}


class CreateQuotaRequest(BaseModel):
    scope: str
    scope_id: str = ""
    limit_usd: float
    period: str = "monthly"
    enabled: bool = True


class UpdateQuotaRequest(BaseModel):
    scope: str | None = None
    scope_id: str | None = None
    limit_usd: float | None = None
    period: str | None = None
    enabled: bool | None = None


def _validate_scope(scope: str) -> None:
    if scope not in _VALID_SCOPES:
        raise HTTPException(status_code=400, detail=f"invalid scope '{scope}' (expected one of {sorted(_VALID_SCOPES)})")


def _validate_period(period: str) -> None:
    if period not in _VALID_PERIODS:
        raise HTTPException(status_code=400, detail=f"invalid period '{period}' (expected one of {sorted(_VALID_PERIODS)})")


@router.get("")
async def list_quotas() -> dict:
    return {"success": True, "data": await quota_store.list_all()}


@router.post("")
async def create_quota(payload: CreateQuotaRequest) -> dict:
    _validate_scope(payload.scope)
    _validate_period(payload.period)
    created = await quota_store.create(
        scope=payload.scope, scope_id=payload.scope_id, limit_usd=payload.limit_usd,
        period=payload.period, enabled=payload.enabled,
    )
    return {"success": True, "data": created}


@router.patch("/{quota_id}")
async def update_quota(quota_id: str, payload: UpdateQuotaRequest) -> dict:
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    if "scope" in fields:
        _validate_scope(fields["scope"])
    if "period" in fields:
        _validate_period(fields["period"])
    updated = await quota_store.set_fields(quota_id, **fields)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"quota '{quota_id}' not found")
    return {"success": True, "data": updated}


@router.delete("/{quota_id}")
async def delete_quota(quota_id: str) -> dict:
    if not await quota_store.delete(quota_id):
        raise HTTPException(status_code=404, detail=f"quota '{quota_id}' not found")
    return {"success": True, "data": {"id": quota_id, "deleted": True}}
