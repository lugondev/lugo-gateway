from fastapi import APIRouter, HTTPException

from app.services.tts.profile_models import TtsProfile
from app.services.tts.profile_store import tts_profile_store

router = APIRouter(prefix="/v1/tts/profiles", tags=["tts"])


@router.get("")
async def list_tts_profiles() -> dict:
    profiles = tts_profile_store.list()
    return {"success": True, "data": {k: v.model_dump() for k, v in profiles.items()}}


@router.post("")
async def create_tts_profile(payload: TtsProfile) -> dict:
    tts_profile_store.upsert(payload)
    return {"success": True, "data": payload.model_dump()}


@router.get("/{name}")
async def get_tts_profile(name: str) -> dict:
    profile = tts_profile_store.get(name)
    if not profile:
        raise HTTPException(status_code=404, detail=f"TTS profile '{name}' not found")
    return {"success": True, "data": profile.model_dump()}


@router.put("/{name}")
async def update_tts_profile(name: str, payload: TtsProfile) -> dict:
    data = payload.model_dump()
    data["name"] = name
    profile = TtsProfile(**data)
    tts_profile_store.upsert(profile)
    return {"success": True, "data": profile.model_dump()}


@router.delete("/{name}")
async def delete_tts_profile(name: str) -> dict:
    tts_profile_store.delete(name)
    return {"success": True, "data": {"name": name, "deleted": True}}
