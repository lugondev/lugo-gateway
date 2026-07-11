from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core.errors import DeviceSerialConflictError, PairingCodeInvalidError
from app.services.auth.devices import device_store
from app.services.auth.pairing import pending_pairings

router = APIRouter(prefix="/v1/devices", tags=["devices"])


class PairInitRequest(BaseModel):
    serial: str


@router.post("/pair/init")
async def pair_init(payload: PairInitRequest) -> dict:
    entry = pending_pairings.create(payload.serial)
    return {"success": True, "data": {"code": entry.code, "poll_token": entry.poll_token}}


@router.get("/pair/status")
async def pair_status(poll_token: str) -> dict:
    entry = pending_pairings.get_by_poll_token(poll_token)
    if entry is None:
        raise HTTPException(status_code=404, detail="pairing session not found or expired")
    if not entry.claimed:
        return {"success": True, "data": {"claimed": False}}
    return {
        "success": True,
        "data": {"claimed": True, "device_id": entry.device_id, "token": entry.token},
    }


class PairClaimRequest(BaseModel):
    code: str
    name: str


@router.post("/pair/claim")
async def pair_claim(payload: PairClaimRequest, request: Request) -> dict:
    entry = pending_pairings.get_by_code(payload.code)
    if entry is None:
        raise PairingCodeInvalidError("pairing code is invalid or expired")
    existing = await device_store.find_active_by_serial(entry.serial)
    if existing is not None:
        raise DeviceSerialConflictError(
            "a device with this hardware is already paired; revoke it first"
        )
    user_id = request.session["user_id"]  # guaranteed by AuthGuardMiddleware on this path
    device, raw_token = await device_store.create(user_id, payload.name, entry.serial)
    pending_pairings.mark_claimed(payload.code, device["id"], raw_token)
    return {"success": True, "data": device}


@router.get("/mine")
async def list_my_devices(request: Request) -> dict:
    user_id = request.session["user_id"]
    return {"success": True, "data": await device_store.list_for_user(user_id)}


@router.post("/mine/{device_id}/revoke")
async def revoke_my_device(device_id: str, request: Request) -> dict:
    user_id = request.session["user_id"]
    ok = await device_store.revoke(device_id, owner_user_id=user_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"device '{device_id}' not found")
    return {"success": True}


@router.get("")
async def list_all_devices() -> dict:
    from app.services.auth.users import user_store

    devices = await device_store.list_all()
    for device in devices:
        owner = await user_store.get_by_id(device["user_id"])
        device["owner_username"] = owner.username if owner else "(deleted)"
    return {"success": True, "data": devices}


@router.post("/{device_id}/revoke")
async def revoke_any_device(device_id: str) -> dict:
    ok = await device_store.revoke(device_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"device '{device_id}' not found")
    return {"success": True}
