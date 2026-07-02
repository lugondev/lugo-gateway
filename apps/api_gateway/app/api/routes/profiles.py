from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.mcp.models import McpServer
from app.services.profiles.models import LlmConfig, Profile, TtsConfig
from app.services.profiles.store import profile_store

router = APIRouter(prefix="/v1/profiles", tags=["profiles"])


def _mask(profile: Profile) -> dict:
    data = profile.model_dump()
    if data.get("llm", {}).get("api_key"):
        data["llm"]["api_key"] = "***"
    return data


class ProfileRequest(BaseModel):
    name: str
    llm: LlmConfig = LlmConfig()
    system_prompt: str = ""
    tts: TtsConfig = TtsConfig()
    mcp_servers: list[McpServer] = []


@router.get("")
async def list_profiles() -> dict:
    profiles = profile_store.list()
    return {"success": True, "data": {k: _mask(v) for k, v in profiles.items()}}


@router.post("")
async def create_profile(payload: ProfileRequest) -> dict:
    profile = Profile(**payload.model_dump())
    profile_store.upsert(profile)
    return {"success": True, "data": profile.model_dump()}


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
    profile = Profile(**data)
    profile_store.upsert(profile)
    return {"success": True, "data": profile.model_dump()}


@router.delete("/{name}")
async def delete_profile(name: str) -> dict:
    profile_store.delete(name)
    return {"success": True, "data": {"name": name, "deleted": True}}
