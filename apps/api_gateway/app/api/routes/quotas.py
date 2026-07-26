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


def _normalize_scope_id(scope: str, scope_id: str, *, require: bool = True) -> str:
    """A scoped quota needs something to scope to; a global one must not carry a
    stray id that would make two identical-looking rows differ.

    `require=False` normalizes without rejecting a blank id -- for a row that
    ends up disabled, where an unusable scope_id enforces nothing (see
    update_quota).
    """
    scope_id = (scope_id or "").strip()
    if scope == "global":
        return ""
    if not scope_id and require:
        raise HTTPException(
            status_code=400,
            detail=f"scope '{scope}' needs a scope_id (the {scope} it applies to)",
        )
    return scope_id


def _validate_limit(limit_usd: float) -> None:
    """The gate only fires on `spend >= limit_usd > 0`, so a limit of 0 or less
    is silently unlimited -- the opposite of what it looks like."""
    if limit_usd is None or limit_usd <= 0:
        raise HTTPException(
            status_code=400,
            detail=(
                f"limit_usd must be greater than 0 (got {limit_usd}); "
                "to allow no spend at all, disable the model or provider instead"
            ),
        )


async def _reject_duplicate(scope: str, scope_id: str, period: str, exclude_id: str = "") -> None:
    """Two rows for the same (scope, scope_id, period) both apply and the
    strictest silently wins, leaving the other as config an admin will misread."""
    for existing in await quota_store.list_all():
        if existing["id"] == exclude_id:
            continue
        if (existing["scope"], existing["scope_id"], existing["period"]) == (scope, scope_id, period):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"a {period} quota for {scope} '{scope_id}' already exists "
                    f"(id {existing['id']}) -- edit that one instead"
                ),
            )


@router.get("")
async def list_quotas() -> dict:
    return {"success": True, "data": await quota_store.list_all()}


@router.post("")
async def create_quota(payload: CreateQuotaRequest) -> dict:
    _validate_scope(payload.scope)
    _validate_period(payload.period)
    scope_id = _normalize_scope_id(payload.scope, payload.scope_id)
    _validate_limit(payload.limit_usd)
    await _reject_duplicate(payload.scope, scope_id, payload.period)
    created = await quota_store.create(
        scope=payload.scope, scope_id=scope_id, limit_usd=payload.limit_usd,
        period=payload.period, enabled=payload.enabled,
    )
    return {"success": True, "data": created}


@router.patch("/{quota_id}")
async def update_quota(quota_id: str, payload: UpdateQuotaRequest) -> dict:
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    existing = await quota_store.get(quota_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"quota '{quota_id}' not found")

    # Validate the row as it will BE, not just the fields that changed: a patch
    # can move a row into an invalid or colliding state one field at a time.
    merged = {**existing, **fields}
    _validate_scope(merged["scope"])
    _validate_period(merged["period"])
    # ...except on a row that ends up DISABLED. The limit and scope_id checks
    # exist so a row can't claim to enforce something it silently won't; a
    # disabled row enforces nothing either way, so failing them can only ever
    # loosen enforcement -- never a reason to refuse. Rows predating those checks
    # (limit_usd=0, or a scoped row with a blank scope_id) would otherwise reject
    # {"enabled": false}, which is all the admin UI's Disable and bulk-disable
    # buttons send: uneditable AND undisableable, i.e. delete-only. Scope/period
    # enums and the duplicate check still apply -- those protect the data shape,
    # not the strength of enforcement.
    enforcing = bool(merged["enabled"])
    merged["scope_id"] = _normalize_scope_id(
        merged["scope"], merged["scope_id"], require=enforcing,
    )
    if enforcing:
        _validate_limit(merged["limit_usd"])
    await _reject_duplicate(
        merged["scope"], merged["scope_id"], merged["period"], exclude_id=quota_id,
    )
    if "scope_id" in fields or fields.get("scope") == "global":
        fields["scope_id"] = merged["scope_id"]

    updated = await quota_store.set_fields(quota_id, **fields)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"quota '{quota_id}' not found")
    return {"success": True, "data": updated}


@router.delete("/{quota_id}")
async def delete_quota(quota_id: str) -> dict:
    if not await quota_store.delete(quota_id):
        raise HTTPException(status_code=404, detail=f"quota '{quota_id}' not found")
    return {"success": True, "data": {"id": quota_id, "deleted": True}}
