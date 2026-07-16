"""Reconstruct the SttLocalConfig/OmnivoiceConfig/RemoteSttConfig shapes from
Model Registry entries instead of SystemConfig. Provider code that already
does `cfg.omnivoice_device` etc. keeps every attribute access unchanged --
only the one line that fetches `cfg` switches to calling a resolver here.

All three resolvers are synchronous and cache-only (see
ModelRegistryStore.find_sync/find_enabled_sync): most call sites run off the
event loop (asyncio.to_thread) or at module-import time, before anything has
awaited the store.
"""

from __future__ import annotations

from app.services.model_registry.store import model_registry_store
from app.services.system_config import OmnivoiceConfig, RemoteSttConfig


# Per-engine engine-level settings that used to be SttLocalConfig fields
# (whisper_local_model, vosk_model_path, whisper tuning, ...). They live in the
# model_id="" sentinel row's config now, next to device/compute_type; these
# defaults apply key-by-key when the row (or a key) is absent. Also the seed
# source for migrate_stt_local_models_to_registry().
STT_ENGINE_CONFIG_DEFAULTS: dict[str, dict] = {
    "whisper_local": {
        "default_model": "phowhisper-medium",
        "vad_filter": True,
        "beam_size": 1,
        "condition_on_previous_text": False,
        "initial_prompt": "",
    },
    "whisper_mlx": {
        "model_path": "models/stt/phowhisper-medium-mlx",
        "condition_on_previous_text": False,
        "initial_prompt": "",
    },
    "qwen3_asr": {"default_model": "Qwen/Qwen3-ASR-0.6B"},
    "vosk": {"model_path": "models/stt/vosk-model-small-en-us-0.15"},
}


def resolve_stt_engine_config(engine: str) -> dict:
    """Engine-level config for a local STT engine (default model, whisper
    decode tuning), merged over the per-engine defaults above. Looked up by
    the reserved model_id="" sentinel -- see resolve_stt_local_device's
    docstring for why the per-model-size governance rows must not match."""
    entry = model_registry_store.find_sync("stt", engine, "")
    config = (entry or {}).get("config") or {}
    return {**STT_ENGINE_CONFIG_DEFAULTS.get(engine, {}), **config}


def resolve_stt_local_device(engine: str) -> dict:
    """{'device': str, 'compute_type': str} for a local STT engine (only
    whisper_local uses compute_type; qwen3_asr's caller just ignores it).
    Looked up by the reserved model_id="" sentinel, which is distinct from
    the per-model-size governance rows seed_known_models() creates under the
    same (kind, engine) pair -- using find_enabled_sync here instead would
    silently match one of those governance rows (empty config) instead."""
    entry = model_registry_store.find_sync("stt", engine, "")
    config = (entry or {}).get("config") or {}
    return {
        "device": config.get("device", ""),
        "compute_type": config.get("compute_type", "int8"),
    }


def resolve_omnivoice_config() -> OmnivoiceConfig:
    """Looked up by the reserved model_id="" sentinel -- see
    resolve_stt_local_device's docstring for why (seed_known_models() creates
    a separate tts/omnivoice/omnivoice governance row that would otherwise be
    ambiguous with this config row)."""
    entry = model_registry_store.find_sync("tts", "omnivoice", "")
    if entry is None:
        return OmnivoiceConfig()
    config = entry.get("config") or {}
    return OmnivoiceConfig().model_copy(update=config)


def resolve_remote_stt_config() -> RemoteSttConfig:
    whisper = model_registry_store.find_enabled_sync("stt", "whisper_service")
    eventlab = model_registry_store.find_enabled_sync("stt", "eventlab")
    cfg = RemoteSttConfig()
    if whisper:
        cfg = cfg.model_copy(update={
            "whisper_service_base_url": whisper.get("base_url", ""),
            "whisper_service_api_key": whisper.get("api_key", ""),
            "whisper_service_model": whisper.get("model_id") or "whisper-1",
        })
    if eventlab:
        cfg = cfg.model_copy(update={
            "eventlab_base_url": eventlab.get("base_url", ""),
            "eventlab_api_key": eventlab.get("api_key", ""),
            "eventlab_model": eventlab.get("model_id") or "whisper-1",
        })
    whisper_timeout = (whisper or {}).get("config", {}).get("timeout_seconds")
    eventlab_timeout = (eventlab or {}).get("config", {}).get("timeout_seconds")
    timeout = whisper_timeout if whisper_timeout is not None else eventlab_timeout
    if timeout is not None:
        cfg = cfg.model_copy(update={"remote_stt_timeout_seconds": timeout})
    return cfg
