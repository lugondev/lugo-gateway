"""Extra TTS engines ported from OmniVoice-Studio (top-5, CPU/MPS-reasonable).

Each engine lazily imports its package and runs real inference following the
OmniVoice-Studio adapter; if the package isn't installed it degrades to silent
mock audio and reports an install hint. Engines:

- kittentts   — English, ONNX CPU, ~80 MB, 8 preset voices.       pip install kittentts
- kokoro      — MLX-Audio / Kokoro 82M, Apple-Silicon, multilingual. pip install mlx-audio
- voxcpm2     — 30 langs, CPU/MPS, 48 kHz, clone + voice design.   pip install voxcpm
- moss-tts-nano — 20 langs, CPU realtime, 48 kHz (clone).          git+OpenMOSS/MOSS-TTS-Nano
- sherpa-onnx — universal ONNX runtime (VITS/Piper/…).            pip install sherpa-onnx
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
from app.services.tts.base import MockFallbackTTSProvider

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


class _ExtraTTSProvider(MockFallbackTTSProvider):
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


class KittenTTSProvider(_ExtraTTSProvider):
    name = "kittentts"
    sample_rate = 24000
    _modules = ("kittentts",)
    _hint = "pip install kittentts + brew install espeak  (ONNX CPU, English, needs espeak phonemizer)"
    PRESET_VOICES = [
        "expr-voice-2-m", "expr-voice-2-f", "expr-voice-3-m", "expr-voice-3-f",
        "expr-voice-4-m", "expr-voice-4-f", "expr-voice-5-m", "expr-voice-5-f",
    ]

    def available(self) -> bool:
        import shutil

        has_espeak = bool(shutil.which("espeak") or shutil.which("espeak-ng"))
        return super().available() and has_espeak

    def detail(self) -> str:
        return os.environ.get("OMNIVOICE_KITTENTTS_MODEL", "KittenML/kitten-tts-mini-0.8")

    def list_voices(self) -> list[dict]:
        return [{"label": v, "voice": v} for v in self.PRESET_VOICES]

    def _model(self):
        if self.name not in _CACHE:
            from kittentts import KittenTTS

            _CACHE[self.name] = KittenTTS(self.detail())
        return _CACHE[self.name]

    def _generate_f32(self, payload: TTSRequest) -> np.ndarray:
        voice = payload.voice if payload.voice in self.PRESET_VOICES else "expr-voice-2-f"
        return _to_mono_f32(self._model().generate(payload.text, voice=voice, speed=float(payload.speed or 1.0)))


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


class MossTTSNanoProvider(_ExtraTTSProvider):
    name = "moss-tts-nano"
    sample_rate = 48000
    _modules = ("transformers", "moss_tts_nano")
    _hint = "git clone OpenMOSS/MOSS-TTS-Nano && pip install -e .  (not on PyPI)"

    def detail(self) -> str:
        return os.environ.get("OMNIVOICE_MOSS_TTS_MODEL", "OpenMOSS-Team/MOSS-TTS-Nano")

    def _model(self):
        if self.name not in _CACHE:
            from moss_tts_nano import MossTTSNano

            _CACHE[self.name] = MossTTSNano.from_pretrained(self.detail(), trust_remote_code=True)
        return _CACHE[self.name]

    def _generate_f32(self, payload: TTSRequest) -> np.ndarray:
        return _to_mono_f32(self._model().generate(text=payload.text, prompt_audio_path=payload.ref_audio_path))


class SherpaOnnxProvider(_ExtraTTSProvider):
    name = "sherpa-onnx"
    sample_rate = 22050
    _modules = ("sherpa_onnx",)
    _hint = "pip install sherpa-onnx + set OMNIVOICE_SHERPA_MODEL to a model dir"

    def detail(self) -> str:
        model_dir = os.environ.get("OMNIVOICE_SHERPA_MODEL", "")
        return f"sherpa-onnx ({model_dir or 'no model dir set'})"

    def _model(self):
        if self.name not in _CACHE:
            import sherpa_onnx

            model_dir = os.environ.get("OMNIVOICE_SHERPA_MODEL", "")
            if not model_dir:
                raise RuntimeError("OMNIVOICE_SHERPA_MODEL not set (sherpa-onnx model dir)")
            config = sherpa_onnx.OfflineTtsConfig(
                model=sherpa_onnx.OfflineTtsModelConfig(
                    vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                        model=os.path.join(model_dir, "model.onnx"),
                        tokens=os.path.join(model_dir, "tokens.txt"),
                    ),
                ),
            )
            tts = sherpa_onnx.OfflineTts(config)
            _CACHE[self.name] = tts
            self.sample_rate = tts.sample_rate
        return _CACHE[self.name]

    def _generate_f32(self, payload: TTSRequest) -> np.ndarray:
        out = self._model().generate(payload.text, sid=0, speed=float(payload.speed or 1.0))
        return _to_mono_f32(out.samples)


EXTRA_TTS_PROVIDERS = [
    KittenTTSProvider(),
    KokoroMLXProvider(),
    VoxCPM2Provider(),
    MossTTSNanoProvider(),
    SherpaOnnxProvider(),
]
