from fastapi import APIRouter, HTTPException, Request

from app.core.actor import current_user_id
from app.core.errors import AuthError, DeviceSerialConflictError, PairingCodeInvalidError
from app.schemas.devices import PairClaimRequest, PairInitRequest
from app.services.auth.devices import device_store
from app.services.auth.pairing import claim_rate_limiter, init_rate_limiter, pending_pairings

router = APIRouter(prefix="/v1/devices", tags=["devices"])


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("/pair/init")
async def pair_init(payload: PairInitRequest, request: Request) -> dict:
    # C3 hardening (defense #3): coarse per-IP limiter -- see pairing.py's
    # module docstring for scope/multi-worker caveat. Keeps an attacker from
    # spinning up unbounded decoy pairings to dilute the burn-after-N defense
    # on pair/claim.
    if not init_rate_limiter.allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="too many pairing attempts, slow down")
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


@router.post("/pair/claim")
async def pair_claim(payload: PairClaimRequest, request: Request) -> dict:
    user_id = current_user_id(request)
    if not user_id:
        raise AuthError("login required")
    # C3 hardening (defense #3): claim requires login, so key the limiter on
    # IP + user_id -- see pairing.py's module docstring for scope/caveat.
    if not claim_rate_limiter.allow(f"{_client_ip(request)}:{user_id}"):
        raise HTTPException(status_code=429, detail="too many pairing attempts, slow down")
    entry = pending_pairings.get_by_code(payload.code)
    if entry is None:
        # C3 hardening (defense #1, primary): a code that matched no live
        # pairing is a failed guess -- charge it against whatever pairing(s)
        # are currently outstanding so a brute-force sweep burns its target
        # long before it could ever reach the real code. See
        # PendingPairingRegistry.record_failed_attempt.
        pending_pairings.record_failed_attempt()
        raise PairingCodeInvalidError("pairing code is invalid or expired")
    existing = await device_store.find_active_by_serial(entry.serial)
    if existing is not None:
        raise DeviceSerialConflictError(
            "a device with this hardware is already paired; revoke it first"
        )
    device, raw_token = await device_store.create(user_id, payload.name, entry.serial)
    pending_pairings.mark_claimed(payload.code, device["id"], raw_token)
    return {"success": True, "data": device}


@router.get("/mine")
async def list_my_devices(request: Request) -> dict:
    user_id = current_user_id(request)
    if not user_id:
        raise AuthError("login required")
    return {"success": True, "data": await device_store.list_for_user(user_id)}


@router.post("/mine/{device_id}/revoke")
async def revoke_my_device(device_id: str, request: Request) -> dict:
    user_id = current_user_id(request)
    if not user_id:
        raise AuthError("login required")
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
