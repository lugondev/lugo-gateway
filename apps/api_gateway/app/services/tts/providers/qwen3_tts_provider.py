"""Qwen3-TTS (0.6B/1.7B) engine — voice clone (Base) + preset speakers (CustomVoice).

Package: qwen-tts (`pip install -U qwen-tts`). Not on this project's core
dependency list — optional, like voxcpm/kokoro-vietnamese; gated by
``available()``. Officially supports 10 languages (not Vietnamese), but
``language="Auto"`` has been verified to produce acceptable Vietnamese
output.
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
        return device, torch.float16, None
    return device, torch.float32, None


def _to_mono_f32(wav) -> np.ndarray:
    arr = np.asarray(wav, dtype=np.float32)
    if arr.ndim == 2:
        arr = arr.mean(axis=0) if arr.shape[0] < arr.shape[1] else arr.mean(axis=1)
    return arr.reshape(-1)


class _Qwen3TTSProviderBase(RenderingTTSProvider):
    """Shared logic for the 0.6B/1.7B Qwen3-TTS engines."""

    _size: str = ""
    _modules = ("qwen_tts", "torch")
    sample_rate = 24000  # overwritten with the real value after the first synth call

    def available(self) -> bool:
        return all(module_available(m) for m in self._modules)

    def install_hint(self) -> str:
        return (
            "pip install -U qwen-tts  "
            "(GPU/CUDA recommended; runs on CPU/MPS but slower, no flash-attention)"
        )

    def detail(self) -> str:
        return f"Qwen3-TTS {self._size} · 12Hz codec · Base+CustomVoice"

    def list_voices(self) -> list[dict]:
        return list(PRESET_SPEAKERS)

    def _checkpoint_id(self, kind: str) -> str:
        return f"Qwen/Qwen3-TTS-12Hz-{self._size}-{kind}"

    def _load_model(self, kind: str):
        key = f"{self.name}:{kind}"
        if key not in _CACHE:
            from qwen_tts import Qwen3TTSModel

            device, dtype, attn = _pick_device_dtype_attn()
            kwargs = {"device_map": device, "dtype": dtype}
            if attn is not None:
                kwargs["attn_implementation"] = attn
            _CACHE[key] = Qwen3TTSModel.from_pretrained(self._checkpoint_id(kind), **kwargs)
        return _CACHE[key]

    def _generate(self, payload: TTSRequest):
        language = payload.language or "Auto"
        if payload.ref_audio_path:
            model = self._load_model("Base")
            return model.generate_voice_clone(
                text=payload.text,
                language=language,
                ref_audio=payload.ref_audio_path,
                ref_text=payload.ref_text,
                x_vector_only_mode=False,
            )
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
