import numpy as np

from app.core.audio import (
    float_array_to_wav_bytes,
    pcm16_to_float_array,
    pcm16_to_wav_bytes,
    silent_wav_bytes,
    wav_duration_seconds,
)


def test_pcm16_to_wav_roundtrip_duration():
    # 16000 samples (32000 bytes) at 16kHz == 1 second.
    pcm = b"\x00\x00" * 16000
    wav = pcm16_to_wav_bytes(pcm, sample_rate=16000)
    assert wav[:4] == b"RIFF"
    assert abs(wav_duration_seconds(wav) - 1.0) < 1e-6


def test_float_array_to_wav_clips_and_sizes():
    samples = np.array([0.0, 1.5, -1.5, 0.5], dtype=np.float32)
    wav = float_array_to_wav_bytes(samples, sample_rate=24000)
    assert wav[:4] == b"RIFF"
    assert abs(wav_duration_seconds(wav) - (4 / 24000)) < 1e-6


def test_silent_wav_has_expected_duration():
    wav = silent_wav_bytes(0.5, sample_rate=24000)
    assert abs(wav_duration_seconds(wav) - 0.5) < 1e-3


def test_pcm16_to_float_array_range():
    pcm = np.array([0, 32767, -32768], dtype="<i2").tobytes()
    arr = pcm16_to_float_array(pcm)
    assert arr.dtype == np.float32
    assert -1.0 <= arr.min() and arr.max() <= 1.0
