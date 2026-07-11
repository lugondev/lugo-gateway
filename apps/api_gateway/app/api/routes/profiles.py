from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

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


@router.get("")
async def list_profiles() -> dict:
    profiles = profile_store.list()
    return {"success": True, "data": {k: _mask(v) for k, v in profiles.items()}}


@router.post("")
async def create_profile(payload: ProfileRequest) -> dict:
    profile = Profile(**payload.model_dump())
    _validate_stt_model(profile)
    profile_store.upsert(profile)
    return {"success": True, "data": _mask(profile)}


@router.get("/{name}")
async def get_profile(name: str) -> dict:
    profile = profile_store.get(name)
    if not profile:
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    return {"success": True, "data": _mask(profile)}


@router.put("/{name}")
async def update_profile(name: str, payload: ProfileRequest) -> dict:
    data = payload.model_dump()
    data["name"] = name
    # Preserve the stored API key when the client sends a blank one: the UI's
    # password field is empty on edit ("leave blank to keep existing"), so a save
    # that doesn't re-enter the key must NOT wipe it.
    if not data.get("llm", {}).get("api_key"):
        existing = profile_store.get(name)
        if existing and existing.llm.api_key:
            data.setdefault("llm", {})["api_key"] = existing.llm.api_key
    profile = Profile(**data)
    _validate_stt_model(profile)
    profile_store.upsert(profile)
    return {"success": True, "data": _mask(profile)}


@router.delete("/{name}")
async def delete_profile(name: str) -> dict:
    profile_store.delete(name)
    return {"success": True, "data": {"name": name, "deleted": True}}
