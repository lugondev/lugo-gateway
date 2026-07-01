"""STT on Apple Silicon GPU via MLX (mlx-whisper).

CTranslate2 (faster-whisper) is CPU-only on macOS; MLX runs Whisper on the Metal
GPU. On an M-series chip this is ~7x faster than the CPU int8 path at equal/better
accuracy (float16). Uses a locally converted PhoWhisper-MLX model so Vietnamese
accuracy matches the faster-whisper PhoWhisper engine.

Available only when `mlx_whisper` is installed (macOS/Apple Silicon) and the model
directory exists; otherwise the engine is hidden and callers fall back to whisper.
"""

import asyncio
import os
import tempfile

from app.core.settings import settings
from app.schemas.stt import STTResult
from app.services.stt.base import STTProvider
from app.services.stt.glossary import resolve_initial_prompt


class WhisperMlxProvider(STTProvider):
    name = "whisper_mlx"

    def available(self) -> bool:
        try:
            import mlx_whisper  # noqa: F401
        except ImportError:
            return False
        return os.path.isdir(settings.whisper_mlx_model_path)

    def detail(self) -> str:
        return f"{os.path.basename(settings.whisper_mlx_model_path)} · Apple GPU (MLX)"

    def _transcribe(self, wav_path: str, language: str | None) -> str:
        import mlx_whisper

        result = mlx_whisper.transcribe(
            wav_path,
            path_or_hf_repo=settings.whisper_mlx_model_path,
            language=language,
            condition_on_previous_text=settings.whisper_condition_on_previous_text,
            initial_prompt=resolve_initial_prompt(
                settings.whisper_initial_prompt, settings.stt_glossary_path
            ),
        )
        return (result.get("text") or "").strip()

    def warm(self) -> None:
        """Compile + load the model once so the first real turn isn't slow."""
        import numpy as np

        tmp = ""
        try:
            from app.core.audio import pcm16_to_wav_bytes

            silence = (np.zeros(8000, dtype="<i2")).tobytes()
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(pcm16_to_wav_bytes(silence, sample_rate=16000))
                tmp = f.name
            self._transcribe(tmp, "vi")
        except Exception:  # noqa: BLE001 - warming is best-effort
            pass
        finally:
            if tmp and os.path.isfile(tmp):
                os.unlink(tmp)

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
