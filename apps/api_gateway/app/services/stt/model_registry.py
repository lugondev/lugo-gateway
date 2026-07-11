"""Common model-variant registry for STT engines that support multiple sizes.

Bridges whisper_manager (whisper/whisper_local/whisper_gemma all share the same
process-global active whisper model — see whisper_provider.get_active_whisper_model)
and Qwen3-ASR's own module-level active-model global, so profile-driven model
selection (SttConfig.model) can validate/select/list against either engine the
same way. Engines with a single fixed model (vosk, whisper_mlx, the remote
engines) have no entry here — there's nothing to select.
"""

from app.core.errors import AppError
from app.core.hf_cache import repo_cached
from app.services.stt.providers.qwen3_asr_provider import (
    QWEN3_ASR_MODELS,
    get_active_qwen3_asr_model,
    resolve_qwen3_asr_model,
    set_active_qwen3_asr_model,
)
from app.services.whisper_models import whisper_manager

_QWEN3_LABELS = {
    "0.6b": "Qwen3-ASR 0.6B (fast)",
    "1.7b": "Qwen3-ASR 1.7B (accurate, multilingual)",
}


class Qwen3AsrModelRegistry:
    def validate(self, model_id: str) -> None:
        if (model_id or "").strip().lower() not in QWEN3_ASR_MODELS:
            raise AppError(f"Invalid qwen3_asr model: {model_id!r}")

    def select(self, model_id: str) -> None:
        self.validate(model_id)
        set_active_qwen3_asr_model(model_id)

    def list_models(self) -> list[dict]:
        active_repo = resolve_qwen3_asr_model(get_active_qwen3_asr_model())
        return [
            {
                "id": shorthand,
                "label": _QWEN3_LABELS.get(shorthand, repo),
                "cached": repo_cached(repo),
                "active": repo == active_repo,
            }
            for shorthand, repo in QWEN3_ASR_MODELS.items()
        ]


qwen3_asr_model_registry = Qwen3AsrModelRegistry()

STT_MODEL_REGISTRIES: dict[str, object] = {
    "whisper": whisper_manager,
    "whisper_local": whisper_manager,
    "whisper_gemma": whisper_manager,
    "qwen3_asr": qwen3_asr_model_registry,
}


def apply_stt_model(engine: str, model: str) -> None:
    """Best-effort switch the active model for `engine` to `model`.

    No-op if `model` is empty or `engine` has no registry (e.g. vosk,
    whisper_mlx — single fixed model, nothing to select). Raises AppError via
    the registry's validate() if `model` is set but not a known id for that
    engine — callers decide whether to propagate or catch-and-log.
    """
    if not model:
        return
    registry = STT_MODEL_REGISTRIES.get(engine)
    if registry is not None:
        registry.select(model)
