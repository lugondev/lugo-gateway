import numpy as np

from app.core.audio import pcm16_to_float_array, read_wav
from app.schemas.stt import STTResult
from app.services.stt.base import STTProvider
from app.services.stt.segmented import (
    energy_speech_regions,
    transcribe_long,
    transcribe_segments,
)

SR = 16000


def _tone(ms: int, amp: float) -> np.ndarray:
    n = int(SR * ms / 1000)
    return np.full(n, amp, dtype=np.float32)


# ---- energy_speech_regions -------------------------------------------------


def test_splits_two_regions_on_long_silence():
    samples = np.concatenate([_tone(300, 0.3), _tone(480, 0.0), _tone(300, 0.3)])
    regions = energy_speech_regions(samples, SR, min_silence_ms=400, min_speech_ms=200, pad_ms=0)
    assert len(regions) == 2
    assert regions[0][0] == 0
    # gap between regions reflects the ~480ms of silence
    gap_ms = 1000 * (regions[1][0] - regions[0][1]) / SR
    assert gap_ms >= 400


def test_merges_regions_across_short_gap():
    samples = np.concatenate([_tone(300, 0.3), _tone(150, 0.0), _tone(300, 0.3)])
    regions = energy_speech_regions(samples, SR, min_silence_ms=400, min_speech_ms=200, pad_ms=0)
    assert len(regions) == 1  # 150ms gap < 400ms -> one region


def test_drops_too_short_speech_blip():
    samples = np.concatenate([_tone(90, 0.3), _tone(480, 0.0), _tone(300, 0.3)])
    regions = energy_speech_regions(samples, SR, min_silence_ms=400, min_speech_ms=200, pad_ms=0)
    assert len(regions) == 1  # the 90ms blip is below min_speech_ms


def test_all_silence_returns_no_regions():
    assert energy_speech_regions(_tone(1000, 0.0), SR) == []


def test_all_speech_returns_single_region():
    regions = energy_speech_regions(_tone(1000, 0.3), SR, pad_ms=0)
    assert len(regions) == 1
    assert regions[0][0] == 0


def test_padding_extends_but_clamps_to_bounds():
    samples = np.concatenate([_tone(300, 0.3), _tone(480, 0.0), _tone(300, 0.3)])
    padded = energy_speech_regions(samples, SR, min_silence_ms=400, min_speech_ms=200, pad_ms=100)
    unpadded = energy_speech_regions(samples, SR, min_silence_ms=400, min_speech_ms=200, pad_ms=0)
    assert padded[0][0] == 0  # clamped at start
    assert padded[0][1] > unpadded[0][1]  # trailing pad extends the region
    assert padded[-1][1] <= len(samples)  # clamped at end


# ---- transcribe_segments / transcribe_long ---------------------------------


class FakeProvider(STTProvider):
    """Labels a segment 'A' (quiet) or 'B' (loud) by its mean amplitude."""

    name = "fake"

    async def transcribe_bytes(self, audio_bytes: bytes, language: str | None = None) -> STTResult:
        pcm, _, _, _ = read_wav(audio_bytes)
        amp = float(np.mean(np.abs(pcm16_to_float_array(pcm))))
        # Threshold well clear of any padding-diluted mean (0.1 vs 0.4 tones).
        return STTResult(engine="fake", text="A" if amp < 0.2 else "B", is_final=True, confidence=None)


async def _run_segments():
    samples = np.concatenate([_tone(300, 0.1), _tone(480, 0.0), _tone(300, 0.4)])
    regions = energy_speech_regions(samples, SR, min_silence_ms=400, min_speech_ms=200, pad_ms=0)
    return await transcribe_segments(FakeProvider(), samples, SR, regions)


def test_transcribe_segments_orders_by_time_and_slices_correctly():
    import asyncio

    segs = asyncio.run(_run_segments())
    assert [s.text for s in segs] == ["A", "B"]
    assert segs[0].start == 0.0
    assert segs[1].start > segs[0].end  # monotonic, separated by the silence gap


def test_transcribe_long_joins_segment_text():
    import asyncio

    samples = np.concatenate([_tone(300, 0.1), _tone(480, 0.0), _tone(300, 0.4)])
    result = asyncio.run(
        transcribe_long(FakeProvider(), samples, SR, min_silence_ms=400, min_speech_ms=200)
    )
    assert result.text == "A B"
    assert result.is_final is True
