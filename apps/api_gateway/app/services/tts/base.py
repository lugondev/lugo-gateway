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

    async def list_voices(self) -> list[dict]:
        """Preset voices as [{"label":..., "voice":...}], or [] if none.

        Async so a remote/proxy engine (see HttpTtsProvider) can fetch
        this from the deployed service instead of only ever answering for
        in-process engines."""
        return []

    async def supports_voice_clone(self) -> bool:
        """Whether ref_audio_path/ref_text (voice cloning) works for this engine."""
        return False


class RenderingTTSProvider(TTSProvider):
    """Base that runs real synthesis and wraps the WAV as a fetchable artifact.

    Subclasses implement ``_render_wav`` (real synthesis -> WAV bytes). Failures
    surface as ``ProviderError`` instead of degrading to placeholder audio.
    """

    sample_rate: int = 24000

    @abstractmethod
    async def _render_wav(self, payload: TTSRequest) -> bytes:
        raise NotImplementedError

    async def render_wav(self, payload: TTSRequest) -> bytes:
        """Public seam: real synthesis -> WAV bytes, no artifact side effect.

        apps/model_service returns these bytes straight on the HTTP response;
        synthesize() below is the gateway's artifact-saving path on top."""
        try:
            return await self._render_wav(payload)
        except Exception as exc:  # noqa: BLE001 - surface as a clean provider error
            raise ProviderError(f"{self.name} synthesis failed: {exc}") from exc

    async def synthesize(self, payload: TTSRequest) -> TTSResult:
        wav = await self.render_wav(payload)

        _, audio_url = artifact_store.save_wav(wav)
        return TTSResult(
            engine=self.name,
            sample_rate=self.sample_rate,
            audio_url=audio_url,
            duration_seconds=round(wav_duration_seconds(wav), 3),
            text=payload.text,
        )
