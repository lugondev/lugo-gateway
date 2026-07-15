import logging
from abc import ABC, abstractmethod

from app.core.audio import wav_duration_seconds
from app.core.errors import ProviderError
from app.schemas.tts import TTSRequest, TTSResult
from app.services.artifacts import artifact_store

logger = logging.getLogger(__name__)


class TTSProvider(ABC):
    name: str

    # Key into install_manager.ALLOWLIST for a one-click UI install; None when the
    # engine isn't a plain `pip install <pkg>` (e.g. gated by a config path, or
    # installed from a git URL rather than a PyPI name).
    install_package: str | None = None

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


class RenderingTTSProvider(TTSProvider):
    """Base that runs real synthesis and wraps the WAV as a fetchable artifact.

    Subclasses implement ``_render_wav`` (real synthesis -> WAV bytes). Failures
    surface as ``ProviderError`` instead of degrading to placeholder audio.
    """

    sample_rate: int = 24000

    @abstractmethod
    async def _render_wav(self, payload: TTSRequest) -> bytes:
        raise NotImplementedError

    async def synthesize(self, payload: TTSRequest) -> TTSResult:
        try:
            wav = await self._render_wav(payload)
        except Exception as exc:  # noqa: BLE001 - surface as a clean provider error
            raise ProviderError(f"{self.name} synthesis failed: {exc}") from exc

        _, audio_url = artifact_store.save_wav(wav)
        return TTSResult(
            engine=self.name,
            sample_rate=self.sample_rate,
            audio_url=audio_url,
            duration_seconds=round(wav_duration_seconds(wav), 3),
            text=payload.text,
        )
