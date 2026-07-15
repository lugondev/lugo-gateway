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


def resolve_stt_local_device(engine: str) -> dict:
    """{'device': str, 'compute_type': str} for a local STT engine (only
    whisper_local uses compute_type; qwen3_asr's caller just ignores it)."""
    entry = model_registry_store.find_enabled_sync("stt", engine)
    config = (entry or {}).get("config") or {}
    return {
        "device": config.get("device", ""),
        "compute_type": config.get("compute_type", "int8"),
    }


def resolve_omnivoice_config() -> OmnivoiceConfig:
    entry = model_registry_store.find_enabled_sync("tts", "omnivoice")
    if entry is None:
        return OmnivoiceConfig()
    config = entry.get("config") or {}
    return OmnivoiceConfig(omnivoice_model_id=entry["model_id"]).model_copy(update=config)


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
