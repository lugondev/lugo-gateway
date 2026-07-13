import os

import pydantic
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel

from app.core.settings import settings
from app.services.artifacts import artifact_store
from app.services.models import model_manager
from app.services.stt.service import stt_service
from app.services.llm_models import llm_manager
from app.services.system_config import SystemConfig, system_config_store
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


class LlmModelRequest(BaseModel):
    model: str


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


def _mask_system_config(config: SystemConfig) -> dict:
    data = config.model_dump()
    if data.get("openrouter_api_key"):
        data["openrouter_api_key"] = "***"
    if data["conversation_llm"].get("conversation_llm_api_key"):
        data["conversation_llm"]["conversation_llm_api_key"] = "***"
    if data["remote_stt"].get("whisper_service_api_key"):
        data["remote_stt"]["whisper_service_api_key"] = "***"
    if data["remote_stt"].get("eventlab_api_key"):
        data["remote_stt"]["eventlab_api_key"] = "***"
    if data["preprocessing"].get("pyannote_auth_token"):
        data["preprocessing"]["pyannote_auth_token"] = "***"
    return data


def _deep_merge(base: dict, overrides: dict) -> dict:
    """Recursively overlay `overrides` onto `base`. A key absent from `overrides`
    keeps its `base` value; a dict value merges key-by-key rather than replacing
    the whole sub-dict, so a partial PUT body (e.g. just `{"base_context": "x"}`)
    never resets sibling groups/fields to their Pydantic defaults."""
    merged = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _merge_system_config(current: SystemConfig, payload: SystemConfig) -> SystemConfig:
    """Blank or '***' in an incoming secret field means "keep the existing value" --
    the UI never re-sends a real secret it fetched, only a fresh one the user typed."""
    update = payload.model_dump()

    def _keep_if_blank_or_masked(new_value: str, old_value: str) -> str:
        return old_value if (not new_value or new_value == "***") else new_value

    update["openrouter_api_key"] = _keep_if_blank_or_masked(
        update["openrouter_api_key"], current.openrouter_api_key
    )
    update["conversation_llm"]["conversation_llm_api_key"] = _keep_if_blank_or_masked(
        update["conversation_llm"]["conversation_llm_api_key"],
        current.conversation_llm.conversation_llm_api_key,
    )
    update["remote_stt"]["whisper_service_api_key"] = _keep_if_blank_or_masked(
        update["remote_stt"]["whisper_service_api_key"],
        current.remote_stt.whisper_service_api_key,
    )
    update["remote_stt"]["eventlab_api_key"] = _keep_if_blank_or_masked(
        update["remote_stt"]["eventlab_api_key"], current.remote_stt.eventlab_api_key
    )
    update["preprocessing"]["pyannote_auth_token"] = _keep_if_blank_or_masked(
        update["preprocessing"]["pyannote_auth_token"],
        current.preprocessing.pyannote_auth_token,
    )
    return SystemConfig.model_validate(update)


@router.get("/system/config")
async def get_system_config() -> dict:
    return {"success": True, "data": _mask_system_config(system_config_store.get())}


@router.put("/system/config")
async def set_system_config(request: Request) -> dict:
    current = system_config_store.get()
    # Accept the raw JSON body (rather than a typed SystemConfig) so we can tell
    # "field absent from the PUT body" apart from "field present with its
    # Pydantic default value" -- a partial body (e.g. the base-context/openrouter
    # save buttons, which only ever send those 2 fields) must never reset the
    # other groups back to their hard-coded defaults.
    try:
        raw = await request.json()
        if not isinstance(raw, dict):
            raise ValueError("request body must be a JSON object")
        deep_merged = _deep_merge(current.model_dump(), raw)
        payload = SystemConfig.model_validate(deep_merged)
    except (ValueError, pydantic.ValidationError) as exc:
        # Mirror the structured 422 FastAPI would give automatically for a typed
        # `payload: SystemConfig` parameter -- manual json()/model_validate() calls
        # don't get that for free, so surface the same status/shape ourselves
        # instead of letting a malformed body fall through as a bare 500.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    merged = _merge_system_config(current, payload)
    new_config = system_config_store.set(merged)
    # Cache-invalidation hooks for settings cached at boot/first-use are added here
    # incrementally (remote_stt in Task 6, omnivoice in Task 7, preprocessing.pyannote_*
    # and stt_local.qwen3_asr_device in Tasks 5/4 respectively).
    return {"success": True, "data": _mask_system_config(new_config)}


@router.get("/models")
async def list_models() -> dict:
    tts = tts_model_manager.snapshot()
    # Per-engine availability (is the engine PACKAGE installed?) so the UI can show
    # the truth — "selected" / "weights downloaded" are not the same as "engine ready".
    tts_engines = {e["engine"]: e for e in tts_service.list_engines()}
    return {
        "success": True,
        "data": {
            "vosk": model_manager.snapshot(),
            "whisper": whisper_manager.snapshot(),
            "omnivoice": tts["omnivoice"],
            "vieneu": tts["vieneu"],
            "llm": llm_manager.snapshot(),
            "tts_engines": tts_engines,
            "install_enabled": settings.allow_runtime_install,
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


# ---- Conversation LLM (Ollama) ----
@router.post("/models/llm/download")
async def download_llm(payload: LlmModelRequest, background: BackgroundTasks) -> dict:
    llm_manager.validate(payload.model)
    background.add_task(llm_manager.download, payload.model)
    return {"success": True, "data": {"model": payload.model, "state": "queued"}}


@router.post("/models/llm/start")
async def start_llm() -> dict:
    return {"success": True, "data": await llm_manager.start_service()}


@router.post("/models/llm/stop")
async def stop_llm() -> dict:
    return {"success": True, "data": await llm_manager.stop()}


@router.post("/models/llm/select")
async def select_llm(payload: LlmModelRequest) -> dict:
    llm_manager.select(payload.model)
    return {"success": True, "data": {"active": payload.model}}


@router.post("/models/llm/delete")
async def delete_llm(payload: LlmModelRequest) -> dict:
    await llm_manager.delete(payload.model)
    return {"success": True, "data": {"model": payload.model, "state": "deleted"}}
