import asyncio
import logging

from app.core.audio import float_array_to_wav_bytes
from app.core.deps import module_available
from app.core.settings import settings
from app.schemas.tts import TTSRequest
from app.services.tts.base import MockFallbackTTSProvider

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 48000  # VieNeu v3 turbo output rate.
_CACHE: dict[str, object] = {}

# Runtime-selected VieNeu mode; falls back to settings/default. Reset on restart.
_active_mode: str | None = None


def get_active_vieneu_mode() -> str:
    return _active_mode or "v3turbo"


def set_active_vieneu_mode(mode: str) -> None:
    global _active_mode
    _active_mode = mode


class VieNeuProvider(MockFallbackTTSProvider):
    name = "vieneu"
    sample_rate = _SAMPLE_RATE

    def available(self) -> bool:
        return module_available("vieneu")

    def detail(self) -> str:
        return f"{get_active_vieneu_mode()} · 48kHz · Vietnamese"

    def _model(self):
        mode = get_active_vieneu_mode()
        if mode not in _CACHE:
            from vieneu import Vieneu

            _CACHE[mode] = Vieneu(mode=mode)
        return _CACHE[mode]

    def list_voices(self) -> list[dict]:
        if not self.available():
            return []
        try:
            voices = self._model().list_preset_voices()
        except Exception as exc:  # noqa: BLE001
            logger.warning("VieNeu list_preset_voices failed: %s", exc)
            return []
        return [{"label": label, "voice": voice_id} for label, voice_id in voices]

    def _generate_wav(self, payload: TTSRequest) -> bytes:
        model = self._model()
        audio = model.infer(
            payload.text,
            ref_audio=payload.ref_audio_path,
            ref_text=payload.ref_text,
            voice=payload.voice or (settings.default_tts_engine_voice or None),
        )
        return float_array_to_wav_bytes(audio, sample_rate=_SAMPLE_RATE)

    async def _render_wav(self, payload: TTSRequest) -> bytes:
        return await asyncio.to_thread(self._generate_wav, payload)
