"""Audio-native STT via Qwen3-Omni (mlx-vlm, Apple GPU).

Unlike Whisper (audio -> text transcription only), Qwen-Omni is a multimodal LLM
that ingests audio directly. Here we use it as an ASR stage: audio -> Vietnamese
text (the speech-output limitation of Qwen-Omni doesn't matter — VieNeu handles
Vietnamese TTS). Understands accent/noise/context better than Whisper, at the cost
of a much heavier model (30B MoE) and higher latency.

Available only when mlx-vlm is installed (Apple Silicon) and the selected model is
cached; otherwise the engine hides and callers fall back to whisper/whisper_mlx.
"""

import asyncio
import os
import tempfile

from app.core.settings import settings
from app.schemas.stt import STTResult
from app.services.stt.base import STTProvider

# Cache loaded (model, processor, config) per model id — loads are very expensive.
_MODEL_CACHE: dict[str, tuple] = {}
_active_model: str | None = None


def get_active_qwen_omni_model() -> str:
    return _active_model or settings.qwen_omni_model


def set_active_qwen_omni_model(model: str) -> None:
    global _active_model
    _active_model = model


def _is_cached(model_id: str) -> bool:
    from app.core.hf_cache import hub_dir

    hub = hub_dir()
    if not hub.is_dir():
        return False
    target = f"models--{model_id.replace('/', '--')}"
    return (hub / target).is_dir()


class QwenOmniProvider(STTProvider):
    name = "qwen_omni"

    def available(self) -> bool:
        try:
            import mlx_vlm  # noqa: F401
        except ImportError:
            return False
        return _is_cached(get_active_qwen_omni_model())

    def detail(self) -> str:
        return f"{get_active_qwen_omni_model().split('/')[-1]} · Apple GPU (MLX)"

    def _load(self):
        from mlx_vlm import load

        model_id = get_active_qwen_omni_model()
        if model_id not in _MODEL_CACHE:
            model, processor = load(model_id)
            config = getattr(model, "config", None)
            if config is None:  # fall back to reading the snapshot's config.json
                from mlx_vlm.utils import get_model_path, load_config

                config = load_config(get_model_path(model_id))
            _MODEL_CACHE[model_id] = (model, processor, config)
        return _MODEL_CACHE[model_id]

    def _transcribe(self, wav_path: str) -> str:
        from mlx_vlm import generate
        from mlx_vlm.prompt_utils import apply_chat_template

        model, processor, config = self._load()
        prompt = apply_chat_template(processor, config, settings.qwen_omni_prompt, num_audios=1)
        out = generate(
            model,
            processor,
            prompt,
            audio=[wav_path],
            max_tokens=settings.qwen_omni_max_tokens,
            verbose=False,
        )
        text = getattr(out, "text", out)
        return str(text).strip()

    def warm(self) -> None:
        try:
            self._load()
        except Exception:  # noqa: BLE001 - best-effort warm
            pass

    async def transcribe_bytes(self, audio_bytes: bytes, language: str | None = None) -> STTResult:
        tmp = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(audio_bytes)
                tmp = f.name
            text = await asyncio.to_thread(self._transcribe, tmp)
            return STTResult(engine=self.name, text=text, is_final=True, confidence=None)
        finally:
            if tmp and os.path.isfile(tmp):
                os.unlink(tmp)
