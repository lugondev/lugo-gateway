import asyncio
import importlib.util
import logging

from app.core.audio import float_array_to_wav_bytes, silent_wav_bytes, wav_duration_seconds
from app.core.settings import settings
from app.schemas.tts import TTSRequest, TTSResult
from app.services.artifacts import artifact_store
from app.services.tts.base import TTSProvider

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


class VieNeuProvider(TTSProvider):
    name = "vieneu"

    def available(self) -> bool:
        return importlib.util.find_spec("vieneu") is not None

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

    def _mock_wav(self, payload: TTSRequest) -> bytes:
        word_count = max(1, len(payload.text.split()))
        return silent_wav_bytes(word_count / 2.5, sample_rate=_SAMPLE_RATE)

    async def synthesize(self, payload: TTSRequest) -> TTSResult:
        mock = settings.enable_mock_engines
        if not mock:
            try:
                wav = await asyncio.to_thread(self._generate_wav, payload)
            except Exception as exc:  # noqa: BLE001 - degrade gracefully
                logger.warning("VieNeu unavailable, using mock audio: %s", exc)
                mock = True
                wav = self._mock_wav(payload)
        else:
            wav = self._mock_wav(payload)

        _, audio_url = artifact_store.save_wav(wav)
        return TTSResult(
            engine=self.name,
            sample_rate=_SAMPLE_RATE,
            audio_url=audio_url,
            duration_seconds=round(wav_duration_seconds(wav), 3),
            text=payload.text,
            mock=mock,
        )
