from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core.actor import current_role, current_user_id
from app.core.errors import AppError
from app.services.mcp.models import McpServer
from app.services.profiles.models import LlmConfig, MemoryConfig, Profile, SessionConfig, SttConfig, TtsConfig
from app.services.profiles.store import profile_store
from app.services.stt.model_registry import STT_MODEL_REGISTRIES
from app.services.stt.profile import resolve_stt_profile

router = APIRouter(prefix="/v1/profiles", tags=["profiles"])


def _mask(profile: Profile) -> dict:
    data = profile.model_dump()
    if data.get("llm", {}).get("api_key"):
        data["llm"]["api_key"] = "***"
    return data


def _validate_stt_model(profile: Profile) -> None:
    if not profile.stt.model:
        return
    preset = resolve_stt_profile(profile.stt.profile)
    # Validation scope is intentionally narrowed to explicit stt.engine or stt.profile preset:
    # profiles must be self-contained and not depend on mutable settings.conversation_stt_engine/
    # default_stt_engine, so a profile's validity is independent of deploy-time config.
    engine = profile.stt.engine or (preset[0] if preset else "")
    if not engine:
        raise AppError("stt.model requires stt.engine or a resolvable stt.profile preset")
    registry = STT_MODEL_REGISTRIES.get(engine)
    if registry is None:
        raise AppError(f"engine '{engine}' has no selectable model variants")
    registry.validate(profile.stt.model)


def _visible(profile: Profile, user_id: str | None) -> bool:
    return profile.owner_id is None or profile.owner_id == user_id


def _can_write(profile: Profile, user_id: str | None, role: str) -> bool:
    """Templates (owner_id is None) may only be modified/deleted by an admin;
    a user's own row may only be modified/deleted by that user. Distinct from
    _visible(), which governs read access -- everyone can SEE a template, but
    only an admin can WRITE to one."""
    if profile.owner_id is None:
        return role == "admin"
    return profile.owner_id == user_id


class ProfileRequest(BaseModel):
    name: str
    nickname: str = ""
    llm: LlmConfig = LlmConfig()
    system_prompt: str = ""
    voice_optimized: bool = False
    stt: SttConfig = SttConfig()
    tts: TtsConfig = TtsConfig()
    mcp_servers: list[McpServer] = []
    memory: MemoryConfig = MemoryConfig()
    session: SessionConfig = SessionConfig()


class CloneRequest(BaseModel):
    new_name: str


@router.get("")
async def list_profiles(request: Request) -> dict:
    user_id = current_user_id(request)
    profiles = profile_store.list()
    visible = {k: v for k, v in profiles.items() if _visible(v, user_id)}
    return {"success": True, "data": {k: _mask(v) for k, v in visible.items()}}


@router.post("")
async def create_profile(payload: ProfileRequest, request: Request) -> dict:
    if profile_store.get(payload.name) is not None:
        raise HTTPException(status_code=409, detail=f"'{payload.name}' already exists")
    owner_id = None if current_role(request) == "admin" else current_user_id(request)
    profile = Profile(**payload.model_dump(), owner_id=owner_id)
    _validate_stt_model(profile)
    profile_store.upsert(profile)
    return {"success": True, "data": _mask(profile)}


@router.get("/{name}")
async def get_profile(name: str, request: Request) -> dict:
    profile = profile_store.get(name)
    if not profile or not _visible(profile, current_user_id(request)):
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    return {"success": True, "data": _mask(profile)}


@router.put("/{name}")
async def update_profile(name: str, payload: ProfileRequest, request: Request) -> dict:
    existing = profile_store.get(name)
    # PUT is upsert-or-create (test_update_uses_path_name relies on creating via
    # PUT to a name that doesn't exist yet); ownership scoping only applies when
    # a row already exists and the caller is not authorized to write to it (not
    # merely able to see it -- see _can_write for the template/admin distinction).
    if existing and not _can_write(existing, current_user_id(request), current_role(request)):
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    data = payload.model_dump()
    data["name"] = name
    if existing:
        data["owner_id"] = existing.owner_id
        # Preserve the stored API key when the client sends a blank one: the UI's
        # password field is empty on edit ("leave blank to keep existing"), so a save
        # that doesn't re-enter the key must NOT wipe it.
        if not data.get("llm", {}).get("api_key") and existing.llm.api_key:
            data.setdefault("llm", {})["api_key"] = existing.llm.api_key
    else:
        data["owner_id"] = None if current_role(request) == "admin" else current_user_id(request)
    profile = Profile(**data)
    _validate_stt_model(profile)
    profile_store.upsert(profile)
    return {"success": True, "data": _mask(profile)}


@router.delete("/{name}")
async def delete_profile(name: str, request: Request) -> dict:
    existing = profile_store.get(name)
    if not existing or not _can_write(existing, current_user_id(request), current_role(request)):
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    profile_store.delete(name)
    return {"success": True, "data": {"name": name, "deleted": True}}


@router.post("/{name}/clone")
async def clone_profile(name: str, payload: CloneRequest, request: Request) -> dict:
    user_id = current_user_id(request)
    source = profile_store.get(name)
    if not source or not _visible(source, user_id):
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    if profile_store.get(payload.new_name) is not None:
        raise HTTPException(status_code=409, detail=f"'{payload.new_name}' already exists")
    data = source.model_dump()
    data["name"] = payload.new_name
    data["owner_id"] = user_id
    clone = Profile(**data)
    profile_store.upsert(clone)
    return {"success": True, "data": _mask(clone)}
