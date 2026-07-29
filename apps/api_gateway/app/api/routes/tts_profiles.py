from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core.actor import current_role, current_user_id
from app.services.artifacts import artifact_store
from app.services.auth.users import user_store
from app.services.model_registry.gate import check_model_allowed
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
    return profile.owner_id is None or profile.owner_id == user_id


def _can_write(profile: TtsProfile, user_id: str | None, role: str) -> bool:
    if profile.owner_id is None:
        return role == "admin"
    return profile.owner_id == user_id


class CloneRequest(BaseModel):
    new_name: str


@router.get("")
async def list_tts_profiles(request: Request) -> dict:
    user_id = current_user_id(request)
    profiles = tts_profile_store.list()
    visible = {k: v for k, v in profiles.items() if _visible(v, user_id)}
    return {"success": True, "data": {k: v.model_dump() for k, v in visible.items()}}


@router.post("")
async def create_tts_profile(payload: TtsProfile, request: Request) -> dict:
    if tts_profile_store.get(payload.name) is not None:
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
    if tts_profile_store.get(payload.new_name) is not None:
        raise HTTPException(status_code=409, detail=f"'{payload.new_name}' already exists")
    data = source.model_dump()
    data["name"] = payload.new_name
    data["owner_id"] = user_id
    clone = TtsProfile(**data)
    tts_profile_store.upsert(clone)
    return {"success": True, "data": clone.model_dump()}
