"""Chunked streaming transcription over a batch (non-incremental) STT engine.

Whisper/Qwen decode a whole clip at once — they have no token-streaming decoder.
This wraps such a provider so a live turn still produces *partial* transcripts:
the growing PCM buffer is re-transcribed at a fixed cadence while the user speaks,
so recognition compute overlaps speech instead of landing entirely after the
endpoint. ``finalize`` returns the full transcript, and skips a redundant decode
when no new audio arrived after the last partial.

This is the FunASR "streaming server" idea adapted to batch engines. Naive
re-decode of the whole buffer is O(n²) compute across a turn; keep ``chunk_ms``
coarse (≈1s) so partials stay cheap relative to the win.
"""

from app.core.audio import pcm16_to_wav_bytes
from app.schemas.stt import STTResult
from app.services.stt.base import STTProvider, STTStream


class ChunkedStreamTranscriber(STTStream):
    def __init__(
        self,
        provider: STTProvider,
        sample_rate: int,
        language: str | None,
        chunk_ms: int = 1000,
    ) -> None:
        self._provider = provider
        self._sample_rate = sample_rate
        self._language = language
        self._chunk_bytes = max(1, int(sample_rate * chunk_ms / 1000) * 2)  # PCM16 = 2 bytes/sample
        self._buffer = bytearray()
        self._bytes_since_partial = 0
        # Cache of the last partial so finalize can reuse it (buffer length + text).
        self._last_partial_len = -1
        self._last_partial_text: str | None = None

    async def _transcribe_buffer(self) -> str:
        wav = pcm16_to_wav_bytes(bytes(self._buffer), sample_rate=self._sample_rate)
        result = await self._provider.transcribe_bytes(wav, self._language)
        return (result.text or "").strip()

    async def accept(self, pcm: bytes) -> list[STTResult]:
        self._buffer.extend(pcm)
        self._bytes_since_partial += len(pcm)
        if self._bytes_since_partial < self._chunk_bytes:
            return []
        self._bytes_since_partial = 0
        text = await self._transcribe_buffer()
        self._last_partial_len = len(self._buffer)
        self._last_partial_text = text
        return [STTResult(engine=self._provider.name, text=text, is_final=False, confidence=None)]

    async def finalize(self) -> STTResult | None:
        if not self._buffer:
            return None
        # Reuse the last partial when nothing new arrived since it was computed.
        if self._last_partial_text is not None and self._last_partial_len == len(self._buffer):
            text = self._last_partial_text
        else:
            text = await self._transcribe_buffer()
        return STTResult(engine=self._provider.name, text=text, is_final=True, confidence=None)
