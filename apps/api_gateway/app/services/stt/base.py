from abc import ABC, abstractmethod

from app.core.audio import pcm16_to_wav_bytes
from app.schemas.stt import STTResult


class STTStream(ABC):
    """A live transcription session fed raw PCM16 mono frames."""

    @abstractmethod
    async def accept(self, pcm: bytes) -> list[STTResult]:
        """Feed a PCM frame; return any partial/final results produced."""
        raise NotImplementedError

    @abstractmethod
    async def finalize(self) -> STTResult | None:
        """Flush remaining audio and return the final transcript, if any."""
        raise NotImplementedError

    async def aclose(self) -> None:
        """Release any resources (sockets, tasks). Default no-op; streams that
        hold resources (e.g. a WebSocket) override this. Safe to call anytime."""
        return None


class BufferingStream(STTStream):
    """Fallback streaming for engines without native incremental decoding.

    Accumulates PCM and transcribes the whole buffer on finalize. Emits no
    partials (the underlying engine is batch-only).

    finalize() consumes what it transcribed: an utterance ends at a flush, so
    the audio it was billed for is spent. Keeping it would make a repeated
    `flush` re-send the whole buffer -- another real paid provider request each
    time, while the socket meters only the audio that arrived since the last
    flush -- and would replay the previous utterance's transcript.
    """

    def __init__(
        self, provider: "STTProvider", sample_rate: int, language: str | None,
        model: str | None = None,
    ) -> None:
        self._provider = provider
        self._sample_rate = sample_rate
        self._language = language
        self._model = model
        self._buffer = bytearray()

    async def accept(self, pcm: bytes) -> list[STTResult]:
        self._buffer.extend(pcm)
        return []

    async def finalize(self) -> STTResult | None:
        if not self._buffer:
            return None
        pcm = bytes(self._buffer)
        # Clear BEFORE the (awaited, failable) provider call: a provider error
        # must not leave the same audio queued for the next flush to pay for
        # again, and this is the only await between the two flushes.
        self._buffer.clear()
        wav = pcm16_to_wav_bytes(pcm, sample_rate=self._sample_rate)
        return await self._provider.transcribe_bytes(wav, self._language, model=self._model)


class STTProvider(ABC):
    name: str

    @abstractmethod
    async def transcribe_bytes(
        self, audio_bytes: bytes, language: str | None = None, model: str | None = None
    ) -> STTResult:
        raise NotImplementedError

    def open_stream(
        self, sample_rate: int, language: str | None = None, model: str | None = None
    ) -> STTStream:
        """Open a streaming session. Default buffers and transcribes on finalize."""
        return BufferingStream(self, sample_rate, language, model)
