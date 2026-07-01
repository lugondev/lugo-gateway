"""Chunked streaming transcription for batch (non-incremental) STT engines.

Transcribes the growing buffer at a fixed cadence *during* speech so partial
hypotheses appear while the user is still talking (UI feedback + decode overlaps
speech). ``finalize`` returns the full transcript, reusing the last partial when
no new audio arrived since it was computed (saves a redundant final decode).
"""

import asyncio

import numpy as np

from app.core.audio import pcm16_to_float_array, read_wav
from app.schemas.stt import STTResult
from app.services.stt.base import STTProvider
from app.services.stt.streaming_chunked import ChunkedStreamTranscriber

SR = 16000


def _pcm(ms: int, amp: float = 0.2) -> bytes:
    n = int(SR * ms / 1000)
    return (np.full(n, amp, dtype=np.float32) * 32767).astype("<i2").tobytes()


class CountingProvider(STTProvider):
    """Returns the buffer's sample count as text; counts how often it's called."""

    name = "counting"

    def __init__(self) -> None:
        self.calls = 0

    async def transcribe_bytes(self, audio_bytes: bytes, language: str | None = None) -> STTResult:
        self.calls += 1
        pcm, _, _, _ = read_wav(audio_bytes)
        n = pcm16_to_float_array(pcm).size
        return STTResult(engine=self.name, text=str(n), is_final=True, confidence=None)


def _run(coro):
    return asyncio.run(coro)


async def _feed_and_collect(stream, chunks):
    out = []
    for c in chunks:
        out.extend(await stream.accept(c))
    return out


def test_no_partial_before_chunk_interval():
    prov = CountingProvider()
    stream = ChunkedStreamTranscriber(prov, SR, None, chunk_ms=1000)
    results = _run(_feed_and_collect(stream, [_pcm(500)]))
    assert results == []
    assert prov.calls == 0


def test_partial_emitted_after_chunk_interval():
    prov = CountingProvider()
    stream = ChunkedStreamTranscriber(prov, SR, None, chunk_ms=1000)
    results = _run(_feed_and_collect(stream, [_pcm(600), _pcm(600)]))  # 1200ms >= 1000
    assert len(results) == 1
    assert results[0].is_final is False
    assert results[0].text  # non-empty partial


def test_partial_transcribes_growing_buffer():
    prov = CountingProvider()
    stream = ChunkedStreamTranscriber(prov, SR, None, chunk_ms=1000)
    results = _run(_feed_and_collect(stream, [_pcm(1000), _pcm(1000)]))
    assert len(results) == 2
    # text is the sample count -> the second partial covers more audio than the first
    assert int(results[1].text) > int(results[0].text)


def test_finalize_returns_final_transcript():
    prov = CountingProvider()
    stream = ChunkedStreamTranscriber(prov, SR, None, chunk_ms=1000)
    _run(_feed_and_collect(stream, [_pcm(500)]))  # below the partial interval
    final = _run(stream.finalize())
    assert final is not None
    assert final.is_final is True
    assert int(final.text) == int(SR * 500 / 1000)


def test_finalize_empty_returns_none():
    prov = CountingProvider()
    stream = ChunkedStreamTranscriber(prov, SR, None, chunk_ms=1000)
    assert _run(stream.finalize()) is None
    assert prov.calls == 0


def test_finalize_reuses_last_partial_when_no_new_audio():
    prov = CountingProvider()
    stream = ChunkedStreamTranscriber(prov, SR, None, chunk_ms=1000)
    _run(_feed_and_collect(stream, [_pcm(1000)]))  # triggers exactly one partial
    assert prov.calls == 1
    final = _run(stream.finalize())  # no new audio since the partial
    assert final is not None and final.is_final is True
    assert prov.calls == 1  # NOT re-decoded — reused the partial
