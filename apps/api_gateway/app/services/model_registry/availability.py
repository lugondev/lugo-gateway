"""Whether a specific (kind, engine, model_id) registry entry corresponds to
an artifact that is actually present on disk. Only applies to local engines
with a real download/delete lifecycle through the Models page (whisper,
vosk, omnivoice, vieneu) -- everything else (service/remote engines,
model_id="" sentinel/engine-config rows, package-only engines with no
per-model weight like edge_tts/qwen3_tts/voxcpm2/whisper_mlx/qwen3_asr) has
no such concept and returns None (not applicable -- the enable-guard must
not block those)."""

from __future__ import annotations


def is_artifact_installed(kind: str, engine: str, model_id: str) -> bool | None:
    if not model_id:
        return None  # sentinel/engine-config row, not a specific artifact
    if model_id == engine:
        # The (engine, engine) shim seed_installed_models_to_registry uses to
        # satisfy the TTS-profile save gate (tts_profiles.py) -- model_id here
        # is a placeholder equal to the engine name, never a real HF repo id
        # or vieneu mode, so there's nothing to look up. Not the same thing
        # as an admin-registered entry with a real model_id.
        return None

    if kind == "stt":
        if engine in ("whisper", "whisper_local"):
            from app.services.whisper_models import whisper_manager
            return any(m["size"] == model_id and m["cached"] for m in whisper_manager.snapshot()["models"])
        if engine == "vosk":
            from app.services.models import model_manager
            return any(m["name"] == model_id for m in model_manager.snapshot()["installed"])
        return None

    if kind == "tts":
        if engine == "omnivoice":
            from app.core.hf_cache import repo_cached
            return repo_cached(model_id)
        if engine == "vieneu":
            from app.core.hf_cache import repo_cached
            from app.services.tts_models import VIENEU_MODES
            repo = next((m["repo"] for m in VIENEU_MODES if m["mode"] == model_id), None)
            return repo_cached(repo) if repo else False
        return None

    return None
