"""Qwen3-TTS (0.6B/1.7B) engine — voice clone (Base) + preset speakers (CustomVoice).

Package: qwen-tts (`pip install -U qwen-tts`). Not on this project's core
dependency list — optional, like voxcpm/kokoro-vietnamese; gated by
``available()``. Officially supports 10 languages (not Vietnamese), but
``language="Auto"`` has been verified to produce acceptable Vietnamese
output.

Optional CUDA fast path: faster-qwen3-tts (`pip install faster-qwen3-tts`,
https://github.com/andimarafioti/faster-qwen3-tts) wraps the same checkpoints
with manual CUDA graph capture for real-time inference. It has NO CPU/MPS
fallback (requires `torch.cuda.CUDAGraph`), so it is only selected when a real
CUDA GPU is present -- on CPU/MPS (e.g. Apple Silicon dev machines) this
engine keeps using the qwen_tts package unchanged. Its non-streaming
generate_voice_clone/generate_custom_voice return the same (audio_list, sr)
shape as qwen_tts, so _generate()/_render_wav() need no further branching once
the right model object is loaded.
"""

import asyncio
import os

import numpy as np

from app.core.audio import float_array_to_wav_bytes
from app.core.deps import module_available
from app.schemas.tts import TTSRequest
from app.services.tts.base import RenderingTTSProvider

_CACHE: dict[str, object] = {}

DEFAULT_SPEAKER = "Vivian"

PRESET_SPEAKERS = [
    {"label": "Vivian (bright young female, Chinese)", "voice": "Vivian"},
    {"label": "Serena (warm young female, Chinese)", "voice": "Serena"},
    {"label": "Uncle_Fu (seasoned male, Chinese)", "voice": "Uncle_Fu"},
    {"label": "Dylan (youthful Beijing male, Chinese)", "voice": "Dylan"},
    {"label": "Eric (lively Sichuan male, Chinese)", "voice": "Eric"},
    {"label": "Ryan (dynamic male, English)", "voice": "Ryan"},
    {"label": "Aiden (sunny American male, English)", "voice": "Aiden"},
    {"label": "Ono_Anna (playful female, Japanese)", "voice": "Ono_Anna"},
    {"label": "Sohee (warm female, Korean)", "voice": "Sohee"},
]


def _pick_device_dtype_attn():
    """Auto-detect device/dtype/attn-impl; ``QWEN3_TTS_DEVICE`` overrides."""
    import torch

    device = os.environ.get("QWEN3_TTS_DEVICE") or (
        "cuda:0"
        if torch.cuda.is_available()
        else "mps"
        if torch.backends.mps.is_available()
        else "cpu"
    )
    if device.startswith("cuda"):
        return device, torch.bfloat16, "flash_attention_2"
    if device == "mps":
        # float16 (NOT float32-alternatives like bfloat16) on MPS makes
        # Qwen3-TTS's nested codec `generate` produce inf/nan in its sampling
        # distribution, and torch.multinomial then raises "probability
        # tensor contains either `inf`, `nan` or element < 0". Empirically
        # reproduced 3/3 with three different input texts; float32 on MPS
        # is confirmed to work (and still uses the Apple GPU — do not
        # "optimize" this back to fp16, and do not fall back to CPU).
        return device, torch.float32, None
    return device, torch.float32, None


def _use_faster_backend(device: str) -> bool:
    """faster_qwen3_tts requires a real CUDA GPU (torch.cuda.CUDAGraph, no
    CPU/MPS fallback) -- select it only when the RESOLVED device (after any
    QWEN3_TTS_DEVICE override) is actually cuda, so forcing cpu/mps via that
    env var also opts out of the CUDA-only package, and it's installed."""
    return device.startswith("cuda") and module_available("faster_qwen3_tts")


def _to_mono_f32(wav) -> np.ndarray:
    if hasattr(wav, "detach"):  # torch tensor
        wav = wav.detach().cpu().numpy()
    arr = np.asarray(wav, dtype=np.float32)
    if arr.ndim == 2:
        arr = arr.mean(axis=0) if arr.shape[0] < arr.shape[1] else arr.mean(axis=1)
    return arr.reshape(-1)


class _Qwen3TTSProviderBase(RenderingTTSProvider):
    """Shared logic for the 0.6B/1.7B Qwen3-TTS engines."""

    _size: str = ""
    _modules = ("qwen_tts", "torch")
    sample_rate = 24000  # overwritten with the real value after the first synth call
    install_package = "qwen_tts"  # shared package for both the 0.6B and 1.7B sizes

    def available(self) -> bool:
        return all(module_available(m) for m in self._modules)

    def install_hint(self) -> str:
        return (
            "pip install -U qwen-tts  "
            "(GPU/CUDA recommended; runs on CPU/MPS but slower, no flash-attention). "
            "On a CUDA GPU, also `pip install faster-qwen3-tts` for a real-time "
            "CUDA-graph fast path (auto-selected; CUDA-only, no CPU/MPS fallback)."
        )

    def detail(self) -> str:
        device, _dtype, _attn = _pick_device_dtype_attn()
        backend = "faster_qwen3_tts (CUDA graphs)" if _use_faster_backend(device) else "qwen_tts"
        return f"Qwen3-TTS {self._size} · 12Hz codec · Base+CustomVoice · {backend}"

    def list_voices(self) -> list[dict]:
        return list(PRESET_SPEAKERS)

    def _checkpoint_id(self, kind: str) -> str:
        return f"Qwen/Qwen3-TTS-12Hz-{self._size}-{kind}"

    def _load_model(self, kind: str):
        key = f"{self.name}:{kind}"
        if key not in _CACHE:
            device, dtype, attn = _pick_device_dtype_attn()
            checkpoint = self._checkpoint_id(kind)
            if _use_faster_backend(device):
                from faster_qwen3_tts import FasterQwen3TTS

                model = FasterQwen3TTS.from_pretrained(checkpoint)
                # Prepares CUDA graphs ahead of the first real request instead
                # of paying that cost on it.
                if hasattr(model, "warmup"):
                    model.warmup()
            else:
                from qwen_tts import Qwen3TTSModel

                kwargs = {"device_map": device, "dtype": dtype}
                if attn is not None:
                    kwargs["attn_implementation"] = attn
                model = Qwen3TTSModel.from_pretrained(checkpoint, **kwargs)
            _CACHE[key] = model
        return _CACHE[key]

    def _generate(self, payload: TTSRequest):
        language = payload.language or "Auto"
        device, _dtype, _attn = _pick_device_dtype_attn()
        is_faster = _use_faster_backend(device)
        if payload.ref_audio_path:
            model = self._load_model("Base")
            kwargs = {
                "text": payload.text,
                "language": language,
                "ref_audio": payload.ref_audio_path,
                "ref_text": payload.ref_text,
            }
            if not is_faster:
                # x_vector_only_mode is a qwen_tts-specific tuning knob;
                # faster_qwen3_tts's generate_voice_clone doesn't accept it.
                kwargs["x_vector_only_mode"] = False
            return model.generate_voice_clone(**kwargs)
        model = self._load_model("CustomVoice")
        return model.generate_custom_voice(
            text=payload.text,
            language=language,
            speaker=payload.voice or DEFAULT_SPEAKER,
            instruct=payload.instruct,
        )

    async def _render_wav(self, payload: TTSRequest) -> bytes:
        wavs, sr = await asyncio.to_thread(self._generate, payload)
        self.sample_rate = int(sr)
        return float_array_to_wav_bytes(_to_mono_f32(wavs[0]), sample_rate=self.sample_rate)


class Qwen3TTS06BProvider(_Qwen3TTSProviderBase):
    name = "qwen3_tts_0_6b"
    _size = "0.6B"


class Qwen3TTS17BProvider(_Qwen3TTSProviderBase):
    name = "qwen3_tts_1_7b"
    _size = "1.7B"


QWEN3_TTS_PROVIDERS = [Qwen3TTS06BProvider(), Qwen3TTS17BProvider()]
