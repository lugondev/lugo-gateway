from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.auth.users import user_store

router = APIRouter(prefix="/v1/users", tags=["users"])

_VALID_ROLES = ("admin", "user")


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "user"


@router.post("")
async def create_user(payload: CreateUserRequest) -> dict:
    if payload.role not in _VALID_ROLES:
        raise HTTPException(status_code=400, detail="role must be 'admin' or 'user'")
    created = await user_store.create(payload.username, payload.password, role=payload.role)
    return {"success": True, "data": created}


@router.get("")
async def list_users() -> dict:
    return {"success": True, "data": await user_store.list()}


class UpdateUserRequest(BaseModel):
    disabled: bool | None = None
    role: str | None = None
    can_use_testing: bool | None = None


@router.patch("/{user_id}")
async def update_user(user_id: str, payload: UpdateUserRequest) -> dict:
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    if "role" in fields and fields["role"] not in _VALID_ROLES:
        raise HTTPException(status_code=400, detail="role must be 'admin' or 'user'")

    demoting = fields.get("role") is not None and fields["role"] != "admin"
    disabling = fields.get("disabled") is True
    if demoting or disabling:
        target = await user_store.get_by_id(user_id)
        if target is not None and target.role == "admin" and not target.disabled:
            if await user_store.count_active_admins() <= 1:
                raise HTTPException(status_code=400, detail="cannot remove the last active admin")

    updated = await user_store.set_fields(user_id, **fields)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"user '{user_id}' not found")
    return {"success": True, "data": updated}


class ResetPasswordRequest(BaseModel):
    new_password: str


@router.post("/{user_id}/reset_password")
async def reset_password(user_id: str, payload: ResetPasswordRequest) -> dict:
    ok = await user_store.reset_password(user_id, payload.new_password)
    if not ok:
        raise HTTPException(status_code=404, detail=f"user '{user_id}' not found")
    return {"success": True}
