import os

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel

from app.core.settings import settings
from app.services.artifacts import artifact_store
from app.services.models import model_manager
from app.services.stt.service import stt_service
from app.services.tts.service import tts_service
from app.services.tts_models import tts_model_manager
from app.services.vad import available_backends
from app.services.whisper_models import whisper_manager

router = APIRouter(prefix="/v1", tags=["system"])


class DownloadRequest(BaseModel):
    name: str


class WhisperRequest(BaseModel):
    size: str


class OmniModelRequest(BaseModel):
    id: str


class VieneuModeRequest(BaseModel):
    mode: str


def _artifacts_stats() -> dict:
    base = artifact_store.base_dir
    files = list(base.glob("*.wav")) if base.is_dir() else []
    total = sum(f.stat().st_size for f in files)
    return {"count": len(files), "total_bytes": total, "dir": str(base)}


@router.get("/system/status")
async def system_status() -> dict:
    from app.services.stt.providers.vosk_provider import get_active_vosk_path

    active_vosk_path = get_active_vosk_path()
    active_whisper = whisper_manager.snapshot()["active"]
    data = {
        "app": {"name": settings.app_name, "env": settings.app_env},
        "stt_engines": stt_service.list_engines(),
        "tts_engines": [{"engine": name} for name in tts_service.providers],
        "tts": {
            "mock_enabled": settings.enable_mock_engines,
            "omnivoice_path": settings.omnivoice_path,
            "omnivoice_present": os.path.isdir(settings.omnivoice_path),
        },
        "whisper_local": {
            "active_model": active_whisper,
            "device": settings.whisper_local_device,
            "cached": whisper_manager._cached(active_whisper),
        },
        "vosk": {
            "active_model_path": active_vosk_path,
            "active_model_present": os.path.isdir(active_vosk_path),
            "installed": model_manager.list_installed(),
        },
        "artifacts": _artifacts_stats(),
        "stream_sample_rate": settings.stt_stream_sample_rate,
        "stt_preprocess": {
            "vad": settings.stt_vad_enabled,
            "vad_backend": settings.stt_vad_backend,
            "vad_backends_available": available_backends(),
            "noise_reduce": settings.stt_noise_reduce_enabled,
            "noise_reduce_amount": settings.stt_noise_reduce_amount,
        },
    }
    return {"success": True, "data": data}


@router.get("/models")
async def list_models() -> dict:
    tts = tts_model_manager.snapshot()
    return {
        "success": True,
        "data": {
            "vosk": model_manager.snapshot(),
            "whisper": whisper_manager.snapshot(),
            "omnivoice": tts["omnivoice"],
            "vieneu": tts["vieneu"],
        },
    }


# ---- Vosk ----
@router.post("/models/vosk/download")
async def download_vosk(payload: DownloadRequest, background: BackgroundTasks) -> dict:
    model_manager.validate(payload.name)
    background.add_task(model_manager.download, payload.name)
    return {"success": True, "data": {"name": payload.name, "state": "queued"}}


@router.delete("/models/vosk/{name}")
async def delete_vosk(name: str) -> dict:
    model_manager.delete(name)
    return {"success": True, "data": {"name": name, "state": "deleted"}}


@router.post("/models/vosk/select")
async def select_vosk(payload: DownloadRequest) -> dict:
    model_manager.select(payload.name)
    return {"success": True, "data": {"active": payload.name}}


# ---- Whisper ----
@router.post("/models/whisper/download")
async def download_whisper(payload: WhisperRequest, background: BackgroundTasks) -> dict:
    whisper_manager.validate(payload.size)
    background.add_task(whisper_manager.download, payload.size)
    return {"success": True, "data": {"size": payload.size, "state": "queued"}}


@router.delete("/models/whisper/{size}")
async def delete_whisper(size: str) -> dict:
    whisper_manager.delete(size)
    return {"success": True, "data": {"size": size, "state": "deleted"}}


@router.post("/models/whisper/select")
async def select_whisper(payload: WhisperRequest) -> dict:
    whisper_manager.select(payload.size)
    return {"success": True, "data": {"active": payload.size}}


# ---- OmniVoice (HF repo id) ----
@router.post("/models/omnivoice/download")
async def download_omnivoice(payload: OmniModelRequest, background: BackgroundTasks) -> dict:
    tts_model_manager.validate_repo(payload.id)
    background.add_task(tts_model_manager.download_omnivoice, payload.id)
    return {"success": True, "data": {"id": payload.id, "state": "queued"}}


@router.post("/models/omnivoice/select")
async def select_omnivoice(payload: OmniModelRequest) -> dict:
    tts_model_manager.select_omnivoice(payload.id)
    return {"success": True, "data": {"active": payload.id}}


@router.post("/models/omnivoice/delete")
async def delete_omnivoice(payload: OmniModelRequest) -> dict:
    tts_model_manager.delete_omnivoice(payload.id)
    return {"success": True, "data": {"id": payload.id, "state": "deleted"}}


# ---- VieNeu (mode) ----
@router.post("/models/vieneu/download")
async def download_vieneu(payload: VieneuModeRequest, background: BackgroundTasks) -> dict:
    tts_model_manager.validate_mode(payload.mode)
    background.add_task(tts_model_manager.download_vieneu, payload.mode)
    return {"success": True, "data": {"mode": payload.mode, "state": "queued"}}


@router.post("/models/vieneu/select")
async def select_vieneu(payload: VieneuModeRequest) -> dict:
    tts_model_manager.select_vieneu(payload.mode)
    return {"success": True, "data": {"active": payload.mode}}


@router.post("/models/vieneu/delete")
async def delete_vieneu(payload: VieneuModeRequest) -> dict:
    tts_model_manager.delete_vieneu(payload.mode)
    return {"success": True, "data": {"mode": payload.mode, "state": "deleted"}}
