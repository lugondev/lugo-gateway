from fastapi import APIRouter, HTTPException, Request

from app.core.actor import current_user_id
from app.core.client_ip import client_ip
from app.core.errors import AuthError, DeviceSerialConflictError, PairingCodeInvalidError
from app.schemas.devices import DeviceProfileRequest, PairClaimRequest, PairInitRequest
from app.services.auth.devices import device_store
from app.services.auth.pairing import claim_rate_limiter, init_rate_limiter, pending_pairings
from app.services.profile_visibility import visible_profile_or_none
from app.services.profiles.store import profile_store

router = APIRouter(prefix="/v1/devices", tags=["devices"])


def _client_ip(request: Request) -> str:
    """Thin alias kept so the two call sites below read the same as before.
    The X-Forwarded-For handling this used to lack now lives in
    app.core.client_ip (see pairing.py's docstring, defense #3)."""
    return client_ip(request)


def _checked_profile_name(profile_id: str, user_id: str) -> str:
    """Return `profile_id` if this user may bind a device to it, else 404.

    This is THE choke point for the bind path. A device identity resolves to its
    owner's user_id (core/auth_guard.resolve_ws_identity), so a binding the owner
    couldn't otherwise see would hand them that profile's llm.api_key,
    system_prompt and private mcp_servers on the next connect -- the C2 IDOR that
    services/profile_visibility.py exists to close. "Belongs to someone else"
    collapses into the same 404 as "doesn't exist", per that module's contract:
    the pair of them must stay indistinguishable so this doesn't become a
    profile-name enumeration oracle."""
    if not profile_id:
        return ""  # unassigned is always allowed
    if visible_profile_or_none(profile_store.get(profile_id), user_id) is None:
        raise HTTPException(status_code=404, detail=f"profile '{profile_id}' not found")
    return profile_id


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
    profile_id = _checked_profile_name(payload.profile_id, user_id)
    device, raw_token = await device_store.create(
        user_id, payload.name, entry.serial, profile_id=profile_id
    )
    pending_pairings.mark_claimed(payload.code, device["id"], raw_token)
    return {"success": True, "data": device}


@router.get("/mine")
async def list_my_devices(request: Request) -> dict:
    user_id = current_user_id(request)
    if not user_id:
        raise AuthError("login required")
    return {"success": True, "data": await device_store.list_for_user(user_id)}


@router.post("/mine/{device_id}/profile")
async def set_my_device_profile(
    device_id: str, payload: DeviceProfileRequest, request: Request
) -> dict:
    """Move a device to another assistant, or unassign it with profile_id="".

    Deliberately does NOT touch the pairing token: the token is hardware
    identity, the profile is a soft setting, so switching assistants must never
    send the user back to the device to read a fresh code off its screen."""
    user_id = current_user_id(request)
    if not user_id:
        raise AuthError("login required")
    profile_id = _checked_profile_name(payload.profile_id, user_id)
    ok = await device_store.set_profile(device_id, profile_id, owner_user_id=user_id)
    if not ok:
        # Someone else's device id and a nonexistent one look identical here, on
        # purpose -- same reasoning as _checked_profile_name.
        raise HTTPException(status_code=404, detail=f"device '{device_id}' not found")
    return {"success": True, "data": {"id": device_id, "profile_id": profile_id}}


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
