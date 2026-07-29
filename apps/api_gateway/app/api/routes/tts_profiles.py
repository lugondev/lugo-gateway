from fastapi import APIRouter, HTTPException, Request

from app.core.actor import current_role, current_user_id
from app.schemas.common import CloneRequest
from app.services.artifacts import artifact_store
from app.services.auth.users import user_store
from app.services.model_registry.gate import check_model_allowed
from app.services.profile_visibility import tts_profile_visible
from app.services.tts.profile_models import TtsProfile
from app.services.tts.profile_store import tts_profile_store

router = APIRouter(prefix="/v1/tts/profiles", tags=["tts"])


async def _resolve_acting_user(request: Request):
    """None when there's no real logged-in user (dev mode, auth fully
    disabled). check_model_allowed handles a None user without crashing."""
    user_id = current_user_id(request)
    return await user_store.get_by_id(user_id) if user_id else None


def _require_ref_audio_path_contained(ref_audio_path: str) -> None:
    """Reject a NEW ref_audio_path outside the artifacts dir at save time,
    with a clear 422 -- deliberately a route-level check, not a TtsProfile
    field_validator, so it never runs against an EXISTING stored row (see
    profile_models.py's module docstring for why that distinction matters:
    a validator on the persisted model would also run at load time and one
    bad/host-mismatched row would break every other profile). Empty string
    is this model's "not set" sentinel, not a path."""
    if not ref_audio_path:
        return
    if not artifact_store.contains(ref_audio_path):
        raise HTTPException(
            status_code=422,
            detail="ref_audio_path must be inside the artifacts directory",
        )


def _visible(profile: TtsProfile, user_id: str | None) -> bool:
    # Delegates to the shared predicate (app/services/profile_visibility.py)
    # so every consumer of a ?tts_profile= name applies the same owner_id
    # rule as this CRUD router.
    return tts_profile_visible(profile, user_id)


def _can_write(profile: TtsProfile, user_id: str | None, role: str) -> bool:
    if profile.owner_id is None:
        return role == "admin"
    return profile.owner_id == user_id


@router.get("")
async def list_tts_profiles(request: Request) -> dict:
    user_id = current_user_id(request)
    profiles = tts_profile_store.list()
    visible = {k: v for k, v in profiles.items() if _visible(v, user_id)}
    return {"success": True, "data": {k: v.model_dump() for k, v in visible.items()}}


@router.post("")
async def create_tts_profile(payload: TtsProfile, request: Request) -> dict:
    # exists(), not `get() is not None`: a name whose row failed to parse
    # (H4) still occupies it -- get() returns None for that name too, and
    # treating that as "free" would let this create silently overwrite the
    # row and hand its ownership to the caller.
    if tts_profile_store.exists(payload.name):
        raise HTTPException(status_code=409, detail=f"'{payload.name}' already exists")
    _require_ref_audio_path_contained(payload.ref_audio_path)
    owner_id = None if current_role(request) == "admin" else current_user_id(request)
    profile = payload.model_copy(update={"owner_id": owner_id})
    if profile.engine:
        acting_user = await _resolve_acting_user(request)
        await check_model_allowed("tts", profile.engine, profile.model_id, acting_user)
    tts_profile_store.upsert(profile)
    return {"success": True, "data": profile.model_dump()}


@router.get("/{name}")
async def get_tts_profile(name: str, request: Request) -> dict:
    profile = tts_profile_store.get(name)
    if not profile or not _visible(profile, current_user_id(request)):
        raise HTTPException(status_code=404, detail=f"TTS profile '{name}' not found")
    return {"success": True, "data": profile.model_dump()}


@router.put("/{name}")
async def update_tts_profile(name: str, payload: TtsProfile, request: Request) -> dict:
    existing = tts_profile_store.get(name)
    # H4: existing is None both when the name is genuinely free AND when its
    # row failed to parse. PUT is upsert-or-create, so falling through in the
    # latter case would skip _can_write entirely (nothing to check ownership
    # against) and silently overwrite the row via the create branch below --
    # must be rejected before that upsert-or-create logic ever runs.
    if existing is None and tts_profile_store.exists(name):
        raise HTTPException(
            status_code=409,
            detail=f"'{name}' exists but its stored data is unreadable; contact an admin",
        )
    # PUT is upsert-or-create (test_update_uses_path_name relies on creating via
    # PUT to a name that doesn't exist yet); ownership scoping only applies when
    # a row already exists and the caller is not authorized to write to it (not
    # merely able to see it -- see _can_write for the template/admin distinction).
    if existing and not _can_write(existing, current_user_id(request), current_role(request)):
        raise HTTPException(status_code=404, detail=f"TTS profile '{name}' not found")
    _require_ref_audio_path_contained(payload.ref_audio_path)
    data = payload.model_dump()
    data["name"] = name
    data["owner_id"] = existing.owner_id if existing else (
        None if current_role(request) == "admin" else current_user_id(request)
    )
    profile = TtsProfile(**data)
    if profile.engine:
        acting_user = await _resolve_acting_user(request)
        await check_model_allowed("tts", profile.engine, profile.model_id, acting_user)
    tts_profile_store.upsert(profile)
    return {"success": True, "data": profile.model_dump()}


@router.delete("/{name}")
async def delete_tts_profile(name: str, request: Request) -> dict:
    existing = tts_profile_store.get(name)
    # H4: distinguish "no such row" (404) from "row exists but is unreadable"
    # (409) -- both leave `existing` None, but only the former is a genuine
    # not-found.
    if existing is None and tts_profile_store.exists(name):
        raise HTTPException(
            status_code=409,
            detail=f"'{name}' exists but its stored data is unreadable; contact an admin",
        )
    if not existing or not _can_write(existing, current_user_id(request), current_role(request)):
        raise HTTPException(status_code=404, detail=f"TTS profile '{name}' not found")
    tts_profile_store.delete(name)
    return {"success": True, "data": {"name": name, "deleted": True}}


@router.post("/{name}/clone")
async def clone_tts_profile(name: str, payload: CloneRequest, request: Request) -> dict:
    user_id = current_user_id(request)
    source = tts_profile_store.get(name)
    if not source or not _visible(source, user_id):
        raise HTTPException(status_code=404, detail=f"TTS profile '{name}' not found")
    # H4: same claim-a-free-name gap as create_tts_profile -- exists(), not
    # `get() is not None`.
    if tts_profile_store.exists(payload.new_name):
        raise HTTPException(status_code=409, detail=f"'{payload.new_name}' already exists")
    data = source.model_dump()
    data["name"] = payload.new_name
    data["owner_id"] = user_id
    clone = TtsProfile(**data)
    # Clone copies engine/model_id straight from the source without going
    # through create/update's model-registry gate -- without this, a user
    # denied a model could still get it by cloning an admin template pinned
    # to it. Same gate, same call shape as create_tts_profile/update_tts_profile.
    if clone.engine:
        acting_user = await _resolve_acting_user(request)
        await check_model_allowed("tts", clone.engine, clone.model_id, acting_user)
    tts_profile_store.upsert(clone)
    return {"success": True, "data": clone.model_dump()}
