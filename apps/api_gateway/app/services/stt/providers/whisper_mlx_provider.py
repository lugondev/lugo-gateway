"""STT on Apple Silicon GPU via MLX (mlx-whisper).

CTranslate2 (faster-whisper) is CPU-only on macOS; MLX runs Whisper on the Metal
GPU. On an M-series chip this is ~7x faster than the CPU int8 path at equal/better
accuracy (float16). Uses a locally converted whisper-MLX model (see
scripts/convert_phowhisper_mlx.sh) matching the faster-whisper `whisper_local`
engine's accuracy.

Available only when `mlx_whisper` is installed (macOS/Apple Silicon) and the model
directory exists; otherwise the engine is hidden and callers fall back to whisper.
"""

import asyncio
import concurrent.futures
import os
import tempfile

from app.schemas.stt import STTResult
from app.services.model_registry.resolve import resolve_stt_engine_config
from app.services.stt.base import STTProvider
from app.services.stt.glossary import resolve_initial_prompt
from app.services.system_config import system_config_store

# mlx_whisper runs on MLX, which is not safe for concurrent cross-thread use (see
# qwen3_asr_provider.py for the same hazard in detail). The conversation warms this
# engine in the background while the user speaks their first turn, so turn-1
# transcribe would otherwise race the warm's model build on a different thread.
# Pinning all MLX work to one dedicated thread makes it single-flight.
_INFER_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="whisper-mlx"
)


class WhisperMlxProvider(STTProvider):
    name = "whisper_mlx"

    def available(self) -> bool:
        try:
            import mlx_whisper  # noqa: F401
        except ImportError:
            return False
        return os.path.isdir(resolve_stt_engine_config("whisper_mlx")["model_path"])

    def detail(self) -> str:
        path = resolve_stt_engine_config("whisper_mlx")["model_path"]
        return f"{os.path.basename(path)} · Apple GPU (MLX)"

    def _transcribe(self, wav_path: str, language: str | None) -> str:
        import mlx_whisper

        engine_cfg = resolve_stt_engine_config("whisper_mlx")
        result = mlx_whisper.transcribe(
            wav_path,
            path_or_hf_repo=engine_cfg["model_path"],
            language=language,
            condition_on_previous_text=engine_cfg["condition_on_previous_text"],
            initial_prompt=resolve_initial_prompt(
                engine_cfg["initial_prompt"],
                system_config_store.get().stt_local.stt_glossary_path,
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
            _INFER_EXECUTOR.submit(self._transcribe, tmp, "vi").result()
        except Exception:  # noqa: BLE001 - warming is best-effort
            pass
        finally:
            if tmp and os.path.isfile(tmp):
                os.unlink(tmp)

    async def transcribe_bytes(
        self, audio_bytes: bytes, language: str | None = None, model: str | None = None
    ) -> STTResult:
        tmp = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(audio_bytes)
                tmp = f.name
            loop = asyncio.get_running_loop()
            text = await loop.run_in_executor(_INFER_EXECUTOR, self._transcribe, tmp, language)
            return STTResult(engine=self.name, text=text, is_final=True, confidence=None)
        finally:
            if tmp and os.path.isfile(tmp):
                os.unlink(tmp)
