"""Extra TTS engines ported from OmniVoice-Studio (CPU/MPS-reasonable).

Each engine lazily imports its package and runs real inference following the
OmniVoice-Studio adapter; if the package isn't installed, ``available()``
reports False and ``install_hint()`` explains how to enable it. Engines:

- kokoro      — MLX-Audio / Kokoro 82M, Apple-Silicon, multilingual. pip install mlx-audio
- voxcpm2     — 30 langs, CPU/MPS, 48 kHz, clone + voice design.   pip install voxcpm
- kokoro_vi   — Kokoro-82M fine-tuned for Vietnamese, CPU, 24 kHz, fixed voicepacks
                (no cloning). Not on PyPI — install from GitHub:
                pip install "kokoro-vietnamese @ git+https://github.com/iamdinhthuan/Kokoro-Vietnamese.git"
"""

import asyncio
import logging
import os
import platform
import sys

import numpy as np

from app.core.audio import float_array_to_wav_bytes
from app.core.deps import module_available
from app.schemas.tts import TTSRequest
from app.services.tts.base import RenderingTTSProvider

logger = logging.getLogger(__name__)

_CACHE: dict[str, object] = {}


def _to_mono_f32(audio) -> np.ndarray:
    """Coerce np/torch/list audio to a 1-D float32 mono array."""
    if hasattr(audio, "detach"):  # torch tensor
        audio = audio.detach().cpu().numpy()
    arr = np.asarray(audio, dtype=np.float32)
    if arr.ndim == 2:
        # (channels, n) or (n, channels) -> mono
        arr = arr.mean(axis=0) if arr.shape[0] < arr.shape[1] else arr.mean(axis=1)
    return arr.reshape(-1)


class _ExtraTTSProvider(RenderingTTSProvider):
    """Shared base for ported engines; subclasses implement _generate_f32 + metadata."""

    _modules: tuple[str, ...] = ()
    _hint: str = ""

    def available(self) -> bool:
        return all(module_available(m) for m in self._modules)

    def install_hint(self) -> str:
        return self._hint

    def _generate_f32(self, payload: TTSRequest) -> np.ndarray:
        raise NotImplementedError

    async def _render_wav(self, payload: TTSRequest) -> bytes:
        audio = await asyncio.to_thread(self._generate_f32, payload)
        return float_array_to_wav_bytes(audio, sample_rate=self.sample_rate)


class KokoroMLXProvider(_ExtraTTSProvider):
    name = "kokoro"
    sample_rate = 24000
    _modules = ("mlx_audio",)
    _hint = "pip install mlx-audio  (Apple Silicon only)"

    def available(self) -> bool:
        if not (sys.platform == "darwin" and platform.machine() == "arm64"):
            return False
        return super().available()

    def detail(self) -> str:
        return os.environ.get("OMNIVOICE_MLX_AUDIO_MODEL", "mlx-community/Kokoro-82M-bf16")

    def _model(self):
        if self.name not in _CACHE:
            from mlx_audio.tts.utils import load_model

            _CACHE[self.name] = load_model(self.detail())
        return _CACHE[self.name]

    def _generate_f32(self, payload: TTSRequest) -> np.ndarray:
        kwargs = {"text": payload.text, "speed": float(payload.speed or 1.0)}
        if payload.voice:
            kwargs["voice"] = payload.voice
        if payload.language:
            kwargs["lang_code"] = payload.language[:2].lower()
        pieces = []
        try:
            results = self._model().generate(**kwargs)
        except TypeError:
            results = self._model().generate(text=payload.text, speed=kwargs["speed"])
        for result in results:
            pieces.append(_to_mono_f32(getattr(result, "audio", result)))
        if not pieces:
            raise RuntimeError("mlx-audio produced no audio")
        return np.concatenate(pieces, axis=-1)


class VoxCPM2Provider(_ExtraTTSProvider):
    name = "voxcpm2"
    sample_rate = 48000
    _modules = ("voxcpm",)
    _hint = "pip install voxcpm  (CPU/MPS; CUDA recommended)"

    def detail(self) -> str:
        return os.environ.get("OMNIVOICE_VOXCPM_MODEL", "openbmb/VoxCPM2")

    def _model(self):
        if self.name not in _CACHE:
            from voxcpm import VoxCPM

            _CACHE[self.name] = VoxCPM.from_pretrained(self.detail(), load_denoiser=False)
        return _CACHE[self.name]

    def _generate_f32(self, payload: TTSRequest) -> np.ndarray:
        model = self._model()
        if payload.instruct and not payload.ref_audio_path:
            wav = model.generate(text=payload.text, voice_description=payload.instruct)
        else:
            prompt = f"({payload.instruct}){payload.text}" if payload.instruct else payload.text
            wav = model.generate(
                text=prompt,
                reference_wav_path=payload.ref_audio_path,
                prompt_wav_path=payload.ref_audio_path if payload.ref_text else None,
                prompt_text=payload.ref_text,
            )
        return _to_mono_f32(wav)


class KokoroVietnameseProvider(_ExtraTTSProvider):
    """Kokoro-82M fine-tuned for Vietnamese (fixed voicepacks, no ref-audio cloning)."""

    name = "kokoro_vi"
    sample_rate = 24000
    _modules = ("kokoro_vietnamese",)
    _hint = (
        'pip install "kokoro-vietnamese @ '
        'git+https://github.com/iamdinhthuan/Kokoro-Vietnamese.git"'
    )

    def detail(self) -> str:
        return "Kokoro-Vietnamese fine-tune · 24kHz · fixed voicepacks"

    def list_voices(self) -> list[dict]:
        if not self.available():
            return []
        from kokoro_vietnamese import VOICES

        return [{"label": v["label"], "voice": voice_id} for voice_id, v in VOICES.items()]

    def _model(self, voice: str | None):
        from kokoro_vietnamese import DEFAULT_VOICE

        voice = voice or DEFAULT_VOICE
        key = f"{self.name}:{voice}"
        if key not in _CACHE:
            from kokoro_vietnamese import KokoroVietnamese

            _CACHE[key] = KokoroVietnamese(device="cpu", voice=voice)
        return _CACHE[key]

    def _generate_f32(self, payload: TTSRequest) -> np.ndarray:
        model = self._model(payload.voice)
        audio, _phonemes = model.synthesize(payload.text, speed=float(payload.speed or 1.0))
        return _to_mono_f32(audio)

    def warm(self) -> None:
        if self.available():
            self._model(None)  # load + cache the default voice so first call is fast


EXTRA_TTS_PROVIDERS = [
    KokoroMLXProvider(),
    VoxCPM2Provider(),
    KokoroVietnameseProvider(),
]
