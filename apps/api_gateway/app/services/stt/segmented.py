"""VAD-segmented parallel transcription for long audio.

FunASR-style pipeline for batch transcription of long clips: split the audio on
silence into speech regions, then transcribe those regions concurrently and merge
the text with per-segment timestamps. Compared to transcribing one long file in a
single pass this gives higher throughput (segments run in parallel) and natural
timestamps, and keeps each decode short (lower peak memory, less drift).

The region splitter is a dependency-free energy gate (same convention as
``app.core.audio.vad_gate``): a frame counts as speech when its RMS is above
``threshold_db`` below the clip peak.
"""

import asyncio
from dataclasses import dataclass

import numpy as np

from app.core.audio import float_array_to_wav_bytes
from app.schemas.stt import STTResult
from app.services.stt.base import STTProvider


def energy_speech_regions(
    samples: np.ndarray,
    sample_rate: int,
    frame_ms: int = 30,
    threshold_db: float = -40.0,
    min_silence_ms: int = 400,
    min_speech_ms: int = 200,
    pad_ms: int = 200,
) -> list[tuple[int, int]]:
    """Return ``(start, end)`` sample ranges of speech, split on silence.

    Frames quieter than ``threshold_db`` below peak are silence. Speech runs
    separated by less than ``min_silence_ms`` are merged; runs shorter than
    ``min_speech_ms`` are dropped; each surviving region is padded by ``pad_ms``
    on both sides (clamped to the clip).
    """
    samples = np.asarray(samples, dtype=np.float32).reshape(-1)
    n = samples.size
    if n == 0:
        return []

    frame = max(1, int(sample_rate * frame_ms / 1000))
    peak = float(np.max(np.abs(samples))) or 1.0
    threshold = peak * (10.0 ** (threshold_db / 20.0))

    # Contiguous runs of speech frames, in sample coordinates.
    runs: list[list[int]] = []
    in_speech = False
    for start in range(0, n, frame):
        seg = samples[start : start + frame]
        rms = float(np.sqrt(np.mean(seg**2))) if seg.size else 0.0
        end = min(n, start + frame)
        if rms >= threshold:
            if in_speech:
                runs[-1][1] = end
            else:
                runs.append([start, end])
                in_speech = True
        else:
            in_speech = False

    if not runs:
        return []

    # Merge runs separated by a gap shorter than min_silence_ms.
    min_gap = int(sample_rate * min_silence_ms / 1000)
    merged: list[list[int]] = [runs[0]]
    for s, e in runs[1:]:
        if s - merged[-1][1] < min_gap:
            merged[-1][1] = e
        else:
            merged.append([s, e])

    # Drop too-short regions, then pad + clamp.
    min_len = int(sample_rate * min_speech_ms / 1000)
    pad = int(sample_rate * pad_ms / 1000)
    out: list[tuple[int, int]] = []
    for s, e in merged:
        if e - s < min_len:
            continue
        out.append((max(0, s - pad), min(n, e + pad)))
    return out


@dataclass
class TranscribedSegment:
    start: float  # seconds
    end: float  # seconds
    text: str


async def transcribe_segments(
    provider: STTProvider,
    samples: np.ndarray,
    sample_rate: int,
    regions: list[tuple[int, int]],
    language: str | None = None,
    concurrency: int = 4,
) -> list[TranscribedSegment]:
    """Transcribe each region concurrently; return non-empty segments in time order."""
    samples = np.asarray(samples, dtype=np.float32).reshape(-1)
    if not regions:
        return []

    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(start: int, end: int) -> str:
        wav = float_array_to_wav_bytes(samples[start:end], sample_rate)
        async with sem:
            result = await provider.transcribe_bytes(wav, language)
        return (result.text or "").strip()

    texts = await asyncio.gather(*(_one(s, e) for s, e in regions))
    segments: list[TranscribedSegment] = []
    for (s, e), text in zip(regions, texts):
        if text:
            segments.append(TranscribedSegment(start=s / sample_rate, end=e / sample_rate, text=text))
    return segments


async def transcribe_long(
    provider: STTProvider,
    samples: np.ndarray,
    sample_rate: int,
    language: str | None = None,
    frame_ms: int = 30,
    threshold_db: float = -40.0,
    min_silence_ms: int = 400,
    min_speech_ms: int = 200,
    pad_ms: int = 200,
    concurrency: int = 4,
) -> STTResult:
    """Split long audio on silence and transcribe the regions in parallel.

    Falls back to a single whole-clip transcription when no speech regions are
    detected (e.g. very short or gapless audio), so behaviour degrades gracefully.
    """
    samples = np.asarray(samples, dtype=np.float32).reshape(-1)
    regions = energy_speech_regions(
        samples,
        sample_rate,
        frame_ms=frame_ms,
        threshold_db=threshold_db,
        min_silence_ms=min_silence_ms,
        min_speech_ms=min_speech_ms,
        pad_ms=pad_ms,
    )
    if not regions:
        wav = float_array_to_wav_bytes(samples, sample_rate)
        return await provider.transcribe_bytes(wav, language)

    segments = await transcribe_segments(
        provider, samples, sample_rate, regions, language=language, concurrency=concurrency
    )
    text = " ".join(seg.text for seg in segments)
    return STTResult(engine=provider.name, text=text, is_final=True, confidence=None)
