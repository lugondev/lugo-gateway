import logging
from abc import ABC, abstractmethod

from app.core.audio import silent_wav_bytes, wav_duration_seconds
from app.core.settings import settings
from app.schemas.tts import TTSRequest, TTSResult
from app.services.artifacts import artifact_store

logger = logging.getLogger(__name__)


class TTSProvider(ABC):
    name: str

    @abstractmethod
    async def synthesize(self, payload: TTSRequest) -> TTSResult:
        raise NotImplementedError

    def available(self) -> bool:
        """Whether the engine can run real synthesis (deps/binaries present)."""
        return True

    def detail(self) -> str:
        """Short model/version label for display."""
        return self.name

    def install_hint(self) -> str:
        """How to enable this engine when it isn't available; empty if built-in."""
        return ""

    def warm(self) -> None:
        """Preload the model so the first synthesis isn't slow. Default: no-op."""
        return None


class MockFallbackTTSProvider(TTSProvider):
    """Base that runs real synthesis and falls back to silent mock audio.

    Subclasses implement ``_render_wav`` (real synthesis -> WAV bytes). The mock
    placeholder is returned when ``ENABLE_MOCK_ENGINES`` is set or real synthesis
    fails, so the pipeline always yields a fetchable artifact.
    """

    sample_rate: int = 24000

    @abstractmethod
    async def _render_wav(self, payload: TTSRequest) -> bytes:
        raise NotImplementedError

    def _mock_wav(self, payload: TTSRequest) -> bytes:
        words = max(1, len(payload.text.split()))
        return silent_wav_bytes(words / 2.5, sample_rate=self.sample_rate)

    async def synthesize(self, payload: TTSRequest) -> TTSResult:
        mock = settings.enable_mock_engines
        if not mock:
            try:
                wav = await self._render_wav(payload)
            except Exception as exc:  # noqa: BLE001 - degrade gracefully, log cause
                logger.warning("%s unavailable, using mock audio: %s", self.name, exc)
                mock = True
                wav = self._mock_wav(payload)
        else:
            wav = self._mock_wav(payload)

        _, audio_url = artifact_store.save_wav(wav)
        return TTSResult(
            engine=self.name,
            sample_rate=self.sample_rate,
            audio_url=audio_url,
            duration_seconds=round(wav_duration_seconds(wav), 3),
            text=payload.text,
            mock=mock,
        )
