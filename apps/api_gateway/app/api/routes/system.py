import os

import pydantic
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel

from app.core.settings import settings
from app.schemas.system import (
    DownloadRequest,
    LlmModelRequest,
    OmniModelRequest,
    VieneuModeRequest,
    WhisperRequest,
)
from app.services.artifacts import artifact_store
from app.services.model_registry.autosync import disable_registry_entry, ensure_registry_entry
from app.services.model_registry.engine_map import registry_ref
from app.services.models import model_manager
from app.services.stt.service import stt_service
from app.services.llm_models import llm_manager
from app.services.system_config import (
    ConversationTuningConfig,
    EngineDefaults,
    KnowledgeServiceConfig,
    PreprocessingConfig,
    SystemConfig,
    system_config_store,
)
from app.services.tts.service import tts_service
from app.services.tts_models import tts_model_manager
from app.services.vad import available_backends
from app.services.whisper_models import whisper_manager

router = APIRouter(prefix="/v1", tags=["system"])


def _artifacts_stats() -> dict:
    base = artifact_store.base_dir
    files = list(base.glob("*.wav")) if base.is_dir() else []
    total = sum(f.stat().st_size for f in files)
    return {"count": len(files), "total_bytes": total, "dir": str(base)}


@router.get("/system/status")
async def system_status() -> dict:
    from app.services.model_registry.resolve import resolve_omnivoice_config, resolve_stt_local_device
    from app.services.stt.providers.vosk_provider import get_active_vosk_path

    active_vosk_path = get_active_vosk_path()
    active_whisper = whisper_manager.snapshot()["active"]
    preprocessing = system_config_store.get().preprocessing
    omnivoice = resolve_omnivoice_config()
    whisper_device_cfg = resolve_stt_local_device("whisper_local")
    data = {
        "app": {"name": settings.app_name, "env": settings.app_env},
        "stt_engines": await stt_service.list_engines(),
        "tts_engines": tts_service.list_engines(),
        "tts": {
            "omnivoice_path": omnivoice.omnivoice_path,
            "omnivoice_present": os.path.isdir(omnivoice.omnivoice_path),
        },
        "whisper_local": {
            "active_model": active_whisper,
            "device": whisper_device_cfg["device"],
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
            "vad": preprocessing.stt_vad_enabled,
            "vad_backend": preprocessing.stt_vad_backend,
            "vad_backends_available": available_backends(),
            "noise_reduce": preprocessing.stt_noise_reduce_enabled,
            "noise_reduce_amount": preprocessing.stt_noise_reduce_amount,
        },
    }
    return {"success": True, "data": data}


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


def _field_meta(model: type[BaseModel]) -> dict[str, dict]:
    meta = {}
    for name, info in model.model_fields.items():
        extra = info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}
        meta[name] = {
            "label": info.title or name,
            "description": info.description or "",
            "subgroup": extra.get("subgroup"),
            "unit": extra.get("unit"),
            "multiline": extra.get("multiline", False),
        }
    return meta


@router.get("/system/config/meta")
async def get_system_config_meta() -> dict:
    return {
        "success": True,
        "data": {
            "engines": _field_meta(EngineDefaults),
            "conversation": _field_meta(ConversationTuningConfig),
            "preprocessing": _field_meta(PreprocessingConfig),
            # Every nested model on SystemConfig must appear here or its fields
            # render in no UI at all -- title/description/subgroup metadata is
            # necessary but not sufficient. test_system_config_meta_route.py
            # derives the expected set from SystemConfig.model_fields so a new
            # group cannot repeat that.
            "knowledge": _field_meta(KnowledgeServiceConfig),
        },
    }


MASK = "***"
# (group, field) holding a credential. knowledge.api_key is the first secret to
# live in SystemConfig; this route used to return model_dump() verbatim, so it
# went out in plaintext. Admin-only is not the same as safe -- admin responses
# reach logs, screenshots and bug reports. Same convention as routes/profiles.py
# (llm.api_key), routes/model_registry.py, routes/providers.py, routes/mcp.py.
_SECRET_FIELDS = (("knowledge", "api_key"),)


def _masked(data: dict) -> dict:
    """model_dump() with every credential replaced by MASK. Mutates the dump,
    which is a fresh dict per call -- never the cached SystemConfig itself."""
    for group, field in _SECRET_FIELDS:
        block = data.get(group)
        if isinstance(block, dict) and block.get(field):
            block[field] = MASK
    return data


def _drop_echoed_masks(raw: dict) -> None:
    """Delete any credential the client sent back as MASK, so the deep merge
    keeps the stored one.

    Read-modify-write is exactly what the admin UI's group Save button does:
    GET the config, change one field, PUT the whole thing. Without this, the
    mask itself gets stored as the real key -- the documented plugin bug, where
    reading /v1/plugins and writing it back overwrote the secret with "***".
    """
    for group, field in _SECRET_FIELDS:
        block = raw.get(group)
        if isinstance(block, dict) and block.get(field) == MASK:
            block.pop(field)


@router.get("/system/config")
async def get_system_config() -> dict:
    return {"success": True, "data": _masked(system_config_store.get().model_dump())}


@router.put("/system/config")
async def set_system_config(request: Request) -> dict:
    current = system_config_store.get()
    # Accept the raw JSON body (rather than a typed SystemConfig) so we can tell
    # "field absent from the PUT body" apart from "field present with its
    # Pydantic default value" -- a partial body (e.g. the base-context save
    # button, which only ever sends that 1 field) must never reset the other
    # groups back to their hard-coded defaults.
    try:
        raw = await request.json()
        if not isinstance(raw, dict):
            raise ValueError("request body must be a JSON object")
        _drop_echoed_masks(raw)
        deep_merged = _deep_merge(current.model_dump(), raw)
        payload = SystemConfig.model_validate(deep_merged)
    except (ValueError, pydantic.ValidationError) as exc:
        # Mirror the structured 422 FastAPI would give automatically for a typed
        # `payload: SystemConfig` parameter -- manual json()/model_validate() calls
        # don't get that for free, so surface the same status/shape ourselves
        # instead of letting a malformed body fall through as a bare 500.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    new_config = system_config_store.set(payload)
    return {"success": True, "data": _masked(new_config.model_dump())}


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
            "llm": await llm_manager.snapshot(),
            "tts_engines": tts_engines,
            "install_enabled": settings.allow_runtime_install,
        },
    }


# ---- Vosk ----
@router.post("/models/vosk/download")
async def download_vosk(payload: DownloadRequest, background: BackgroundTasks) -> dict:
    model_manager.validate(payload.name)
    background.add_task(model_manager.download, payload.name)
    ref = registry_ref("vosk", payload.name)
    if ref:
        await ensure_registry_entry(*ref)
    return {"success": True, "data": {"name": payload.name, "state": "queued"}}


@router.delete("/models/vosk/{name}")
async def delete_vosk(name: str) -> dict:
    model_manager.delete(name)
    kind, engine, model_id, _ = registry_ref("vosk", name)
    await disable_registry_entry(kind, engine, model_id)
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
    ref = registry_ref("whisper", payload.size)
    if ref:
        await ensure_registry_entry(*ref)
    return {"success": True, "data": {"size": payload.size, "state": "queued"}}


@router.delete("/models/whisper/{size}")
async def delete_whisper(size: str) -> dict:
    whisper_manager.delete(size)
    kind, engine, model_id, _ = registry_ref("whisper", size)
    await disable_registry_entry(kind, engine, model_id)
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
    ref = registry_ref("omnivoice", payload.id)
    if ref:
        await ensure_registry_entry(*ref)
    return {"success": True, "data": {"id": payload.id, "state": "queued"}}


@router.post("/models/omnivoice/select")
async def select_omnivoice(payload: OmniModelRequest) -> dict:
    tts_model_manager.select_omnivoice(payload.id)
    return {"success": True, "data": {"active": payload.id}}


@router.post("/models/omnivoice/delete")
async def delete_omnivoice(payload: OmniModelRequest) -> dict:
    tts_model_manager.delete_omnivoice(payload.id)
    kind, engine, model_id, _ = registry_ref("omnivoice", payload.id)
    await disable_registry_entry(kind, engine, model_id)
    return {"success": True, "data": {"id": payload.id, "state": "deleted"}}


# ---- VieNeu (mode) ----
@router.post("/models/vieneu/download")
async def download_vieneu(payload: VieneuModeRequest, background: BackgroundTasks) -> dict:
    tts_model_manager.validate_mode(payload.mode)
    background.add_task(tts_model_manager.download_vieneu, payload.mode)
    ref = registry_ref("vieneu", payload.mode)
    if ref:
        await ensure_registry_entry(*ref)
    return {"success": True, "data": {"mode": payload.mode, "state": "queued"}}


@router.post("/models/vieneu/select")
async def select_vieneu(payload: VieneuModeRequest) -> dict:
    tts_model_manager.select_vieneu(payload.mode)
    return {"success": True, "data": {"active": payload.mode}}


@router.post("/models/vieneu/delete")
async def delete_vieneu(payload: VieneuModeRequest) -> dict:
    tts_model_manager.delete_vieneu(payload.mode)
    kind, engine, model_id, _ = registry_ref("vieneu", payload.mode)
    await disable_registry_entry(kind, engine, model_id)
    return {"success": True, "data": {"mode": payload.mode, "state": "deleted"}}


# ---- Conversation LLM (Ollama) ----
@router.post("/models/llm/download")
async def download_llm(payload: LlmModelRequest, background: BackgroundTasks) -> dict:
    llm_manager.validate(payload.model)
    background.add_task(llm_manager.download, payload.model)
    ref = registry_ref("llm", payload.model)
    if ref:
        await ensure_registry_entry(*ref)
    return {"success": True, "data": {"model": payload.model, "state": "queued"}}


@router.post("/models/llm/start")
async def start_llm() -> dict:
    return {"success": True, "data": await llm_manager.start_service()}


@router.post("/models/llm/stop")
async def stop_llm() -> dict:
    return {"success": True, "data": await llm_manager.stop()}


@router.post("/models/llm/select")
async def select_llm(payload: LlmModelRequest) -> dict:
    await llm_manager.select(payload.model)
    return {"success": True, "data": {"active": payload.model}}


@router.post("/models/llm/delete")
async def delete_llm(payload: LlmModelRequest) -> dict:
    await llm_manager.delete(payload.model)
    kind, engine, model_id, _ = registry_ref("llm", payload.model)
    await disable_registry_entry(kind, engine, model_id)
    return {"success": True, "data": {"model": payload.model, "state": "deleted"}}
