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

import os

from app.services.model_registry.store import model_registry_store
from app.services.providers.resolve import resolve_credentials_sync
from app.services.system_config import OmnivoiceConfig, RemoteSttConfig

# Recognized spellings for a bool-typed env override. Anything else is a
# misconfiguration and must fail loudly -- see EnvVarError below -- rather
# than silently becoming False the way `raw.lower() in (...)` used to.
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class EnvVarError(ValueError):
    """A {PREFIX}_{KEY} env var holds a value that can't be coerced to the
    type its default expects. This is the container's whole config surface
    (see resolve_stt_engine_config's docstring), so a bad value must fail
    loudly and name both the variable and what was expected -- not be
    swallowed into some silently-wrong default."""


def _coerce(raw: str, default, var_name: str):
    """Coerce an env string to the type of the default it overrides. bool is
    checked before int because bool is a subclass of int."""
    if isinstance(default, bool):
        normalized = raw.strip().lower()
        if normalized in _TRUE_VALUES:
            return True
        if normalized in _FALSE_VALUES:
            return False
        raise EnvVarError(
            f"{var_name}={raw!r} is not a valid boolean; expected one of "
            f"{sorted(_TRUE_VALUES | _FALSE_VALUES)}"
        )
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError as exc:
            raise EnvVarError(f"{var_name}={raw!r} is not a valid integer") from exc
    if isinstance(default, float):
        try:
            return float(raw)
        except ValueError as exc:
            raise EnvVarError(f"{var_name}={raw!r} is not a valid float") from exc
    return raw


def _env_overrides(prefix: str, defaults: dict) -> dict:
    """Read {PREFIX}_{KEY} for each key in `defaults`. Only keys that exist in
    `defaults` are readable, so a typo'd env var is ignored rather than
    injecting an unknown key into provider config."""
    out = {}
    for key, default in defaults.items():
        var_name = f"{prefix}_{key}".upper()
        raw = os.environ.get(var_name)
        if raw is not None:
            out[key] = _coerce(raw, default, var_name)
    return out


# Per-engine engine-level settings that used to be SttLocalConfig fields
# (whisper_local_model, vosk_model_path, whisper tuning, ...). They live in the
# model_id="" sentinel row's config now, next to device/compute_type; these
# defaults apply key-by-key when the row (or a key) is absent. Also the seed
# source for migrate_stt_local_models_to_registry().
STT_ENGINE_CONFIG_DEFAULTS: dict[str, dict] = {
    "whisper_local": {
        "default_model": "large-v3-turbo",
        "vad_filter": True,
        "beam_size": 1,
        "condition_on_previous_text": False,
        "initial_prompt": "",
    },
    "whisper_mlx": {
        "model_path": "models/stt/whisper-large-v3-turbo-mlx",
        "condition_on_previous_text": False,
        "initial_prompt": "",
    },
    "qwen3_asr": {"default_model": "Qwen/Qwen3-ASR-0.6B"},
    # GGUF/CPU backend via the qwen3-asr-cli binary. default_model is a filesystem
    # path to a .gguf (not an HF repo); binary_path "" -> auto-locate (PATH / build
    # dir). See qwen3_asr_gguf_provider.py.
    "qwen3_asr_gguf": {
        "default_model": "apps/model_service/vendor/qwen3-asr.cpp/models/qwen3-asr-1.7b-q8_0.gguf",
        "binary_path": "",
        "n_threads": 8,
        "timeout_seconds": 120.0,
    },
    "vosk": {"model_path": "models/stt/vosk-model-small-en-us-0.15"},
}


def resolve_stt_engine_config(engine: str) -> dict:
    """Engine-level config for a local STT engine (default model, whisper
    decode tuning), merged over the per-engine defaults above. Looked up by
    the reserved model_id="" sentinel -- see resolve_stt_local_device's
    docstring for why an arbitrary per-model row must not match.

    Precedence: registry row > env > defaults. The env layer exists for
    apps/model_service, which runs a provider with no registry DB: there the
    cache is cold, find_sync returns None, and env wins. In the gateway a
    sentinel row exists and still wins, so this is a no-op there."""
    defaults = STT_ENGINE_CONFIG_DEFAULTS.get(engine, {})
    entry = model_registry_store.find_sync("stt", engine, "")
    config = (entry or {}).get("config") or {}
    return {**defaults, **_env_overrides(f"STT_{engine}", defaults), **config}


def resolve_stt_local_device(engine: str) -> dict:
    """{'device': str, 'compute_type': str} for a local STT engine (only
    whisper_local uses compute_type; qwen3_asr's caller just ignores it).
    Looked up by the reserved model_id="" sentinel, which is distinct from
    any per-model restriction row an admin may add under the same (kind,
    engine) pair -- using find_enabled_sync here would silently match such a
    row instead of the config sentinel.

    Precedence: registry row > env > defaults (see resolve_stt_engine_config)."""
    defaults = {"device": "", "compute_type": "int8"}
    entry = model_registry_store.find_sync("stt", engine, "")
    config = (entry or {}).get("config") or {}
    merged = {**defaults, **_env_overrides(f"STT_{engine}", defaults), **config}
    # The sentinel row's config also carries engine-level keys (default_model,
    # beam_size, ...); return only this resolver's two.
    return {"device": merged["device"], "compute_type": merged["compute_type"]}


def resolve_omnivoice_config() -> OmnivoiceConfig:
    """Looked up by the reserved model_id="" sentinel -- see
    resolve_stt_local_device's docstring for why (an admin may add a
    tts/omnivoice restriction row that would otherwise be ambiguous with this
    config sentinel)."""
    entry = model_registry_store.find_sync("tts", "omnivoice", "")
    if entry is None:
        return OmnivoiceConfig()
    config = entry.get("config") or {}
    return OmnivoiceConfig().model_copy(update=config)


def resolve_remote_stt_config() -> RemoteSttConfig:
    """Whisper/eventlab entries may have blank own base_url/api_key and carry
    a linked provider (config.provider_id) instead -- route both through the
    sync credential resolver (cache-only, matches this function's sync/
    cache-only contract) so a provider-linked entry still resolves to real
    creds instead of reporting blank."""
    whisper = model_registry_store.find_enabled_sync("stt", "whisper_service")
    eventlab = model_registry_store.find_enabled_sync("stt", "eventlab")
    cfg = RemoteSttConfig()
    if whisper:
        whisper_base_url, whisper_api_key = resolve_credentials_sync(whisper)
        cfg = cfg.model_copy(update={
            "whisper_service_base_url": whisper_base_url,
            "whisper_service_api_key": whisper_api_key,
            "whisper_service_model": whisper.get("model_id") or "whisper-1",
        })
    if eventlab:
        eventlab_base_url, eventlab_api_key = resolve_credentials_sync(eventlab)
        cfg = cfg.model_copy(update={
            "eventlab_base_url": eventlab_base_url,
            "eventlab_api_key": eventlab_api_key,
            "eventlab_model": eventlab.get("model_id") or "whisper-1",
        })
    whisper_timeout = (whisper or {}).get("config", {}).get("timeout_seconds")
    eventlab_timeout = (eventlab or {}).get("config", {}).get("timeout_seconds")
    timeout = whisper_timeout if whisper_timeout is not None else eventlab_timeout
    if timeout is not None:
        cfg = cfg.model_copy(update={"remote_stt_timeout_seconds": timeout})
    return cfg
