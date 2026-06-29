"""Qwen3-ASR STT on Apple Silicon via MLX (mlx-qwen3-asr).

Qwen3-ASR (Alibaba, Apache-2.0) is a strong multilingual ASR model that — unlike
SenseVoice — supports Vietnamese, and beats Whisper-large-v3 on several benchmarks.
This runs the MLX port on Apple GPU (Metal), torch-free. Verified transcribing
Vietnamese correctly on Apple Silicon.

Apple-Silicon only (like whisper_mlx / qwen_omni): the engine auto-hides when
`mlx-qwen3-asr` (the optional `qwen3-asr` extra) is absent — so it never appears on
the Linux deploy. Weights download from the hub on first use.
"""

import asyncio
import os
import tempfile

from app.core.deps import module_available
from app.core.settings import settings
from app.schemas.stt import STTResult
from app.services.stt.base import STTProvider

_MODEL_CACHE: dict[str, object] = {}

# Map our short language codes to the names Qwen3-ASR expects; unknown -> auto-detect.
_LANG = {
    "vi": "Vietnamese", "en": "English", "zh": "Chinese",
    "ja": "Japanese", "ko": "Korean", "yue": "Cantonese",
}


class Qwen3AsrProvider(STTProvider):
    name = "qwen3_asr"

    def available(self) -> bool:
        return module_available("mlx_qwen3_asr")

    def detail(self) -> str:
        return f"{settings.qwen3_asr_model.split('/')[-1]} · Apple GPU (MLX) · multilingual incl. Vietnamese"

    def _session(self):
        """Cache a Session (owns model + tokenizer) per model id — loads are costly."""
        model_id = settings.qwen3_asr_model
        if model_id not in _MODEL_CACHE:
            from mlx_qwen3_asr import Session

            _MODEL_CACHE[model_id] = Session(model_id)
        return _MODEL_CACHE[model_id]

    def _transcribe(self, wav_path: str, language: str | None) -> str:
        session = self._session()
        lang = _LANG.get((language or "").lower())  # None => auto-detect
        result = session.transcribe(wav_path, language=lang)
        return (getattr(result, "text", "") or "").strip()

    def warm(self) -> None:
        try:
            self._session()
        except Exception:  # noqa: BLE001 - best-effort warm
            pass

    async def transcribe_bytes(self, audio_bytes: bytes, language: str | None = None) -> STTResult:
        tmp = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(audio_bytes)
                tmp = f.name
            text = await asyncio.to_thread(self._transcribe, tmp, language)
            return STTResult(engine=self.name, text=text, is_final=True, confidence=None)
        finally:
            if tmp and os.path.isfile(tmp):
                os.unlink(tmp)
