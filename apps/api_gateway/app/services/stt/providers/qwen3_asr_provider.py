"""Qwen3-ASR STT — runs on Apple Silicon (MLX) or NVIDIA GPU (official qwen-asr).

Qwen3-ASR (Alibaba, Apache-2.0) is a strong multilingual ASR model that supports
Vietnamese and beats Whisper-large-v3 on several benchmarks.
Two GPU backends, auto-selected by what's installed:
  - `mlx-qwen3-asr`  -> Apple GPU (Metal), torch-free  (verified VN on Apple Silicon)
  - `qwen-asr`       -> NVIDIA GPU (PyTorch + CUDA)     (e.g. a T4 on Colab)

CPU is not supported by either backend, so the engine auto-hides when neither
package is present (e.g. the CPU-only Linux deploy). Weights download on first use.
"""

import asyncio
import os
import platform
import tempfile

from app.core.deps import module_available
from app.core.settings import settings
from app.schemas.stt import STTResult
from app.services.stt.base import STTProvider

_MODEL_CACHE: dict[str, object] = {}


def _is_apple_silicon() -> bool:
    """MLX only runs on Apple Silicon (libmlx.so is absent on Linux)."""
    return platform.system().lower() == "darwin" and platform.machine().lower() in {"arm64", "aarch64"}


def _cuda_dtype(torch):
    """bf16 only on GPUs that support it (Ampere+); float16 on older ones like the
    NVIDIA T4 (Turing has no native bfloat16)."""
    try:
        return torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    except Exception:  # noqa: BLE001
        return torch.float16

# Map our short language codes to the names Qwen3-ASR expects; unknown -> auto-detect.
_LANG = {
    "vi": "Vietnamese", "en": "English", "zh": "Chinese",
    "ja": "Japanese", "ko": "Korean", "yue": "Cantonese",
}


def _extract_text(out) -> str:
    """Pull transcript text out of whatever the backend returns (str/dict/list/obj)."""
    if out is None:
        return ""
    if isinstance(out, str):
        return out.strip()
    if isinstance(out, dict):
        return str(out.get("text") or out.get("transcription") or "").strip()
    if isinstance(out, (list, tuple)) and out:
        return _extract_text(out[0])
    return (getattr(out, "text", None) or "").strip()


class Qwen3AsrProvider(STTProvider):
    name = "qwen3_asr"

    def _backend(self) -> str | None:
        # MLX only on Apple Silicon — importing mlx on Linux fails with
        # "libmlx.so: cannot open shared object file". So on non-Apple hosts prefer
        # the CUDA backend even if mlx-qwen3-asr happens to be installed.
        if _is_apple_silicon() and module_available("mlx_qwen3_asr"):
            return "mlx"
        if module_available("qwen_asr"):
            return "cuda"
        return None

    def available(self) -> bool:
        return self._backend() is not None

    def detail(self) -> str:
        model = settings.qwen3_asr_model.split("/")[-1]
        backend = self._backend()
        where = {"mlx": "Apple GPU (MLX)", "cuda": "NVIDIA GPU (CUDA)"}.get(backend, "GPU")
        return f"{model} · {where} · multilingual incl. Vietnamese"

    def _mlx_session(self):
        key = f"mlx:{settings.qwen3_asr_model}"
        if key not in _MODEL_CACHE:
            from mlx_qwen3_asr import Session

            _MODEL_CACHE[key] = Session(settings.qwen3_asr_model)
        return _MODEL_CACHE[key]

    def _cuda_model(self):
        key = f"cuda:{settings.qwen3_asr_model}"
        if key not in _MODEL_CACHE:
            import torch
            from qwen_asr import Qwen3ASRModel

            _MODEL_CACHE[key] = Qwen3ASRModel.from_pretrained(
                settings.qwen3_asr_model,
                dtype=_cuda_dtype(torch),  # bf16 on Ampere+, fp16 on T4/Turing
                device_map=settings.qwen3_asr_device or "cuda:0",
                max_new_tokens=256,
            )
        return _MODEL_CACHE[key]

    def _transcribe(self, wav_path: str, language: str | None) -> str:
        backend = self._backend()
        lang = _LANG.get((language or "").lower())  # None => auto-detect
        if backend == "mlx":
            return _extract_text(self._mlx_session().transcribe(wav_path, language=lang))
        if backend == "cuda":
            return _extract_text(self._cuda_model().transcribe(audio=wav_path, language=lang))
        raise RuntimeError("Qwen3-ASR needs mlx-qwen3-asr (Apple) or qwen-asr (CUDA) installed")

    def warm(self) -> None:
        try:
            if self._backend() == "mlx":
                self._mlx_session()
            elif self._backend() == "cuda":
                self._cuda_model()
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
