import numpy as np

from app.core.audio import (
    float_array_to_wav_bytes,
    pcm16_to_wav_bytes,
    preprocess_pcm16,
    preprocess_wav_bytes,
    reduce_noise,
    vad_gate,
    wav_duration_seconds,
)


def _tone(freq, seconds, sr):
    t = np.linspace(0, seconds, int(seconds * sr), endpoint=False)
    return (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def test_reduce_noise_preserves_length_and_is_finite():
    rng = np.random.default_rng(0)
    signal = _tone(220, 1.0, 16000) + 0.05 * rng.standard_normal(16000).astype(np.float32)
    out = reduce_noise(signal)
    assert out.shape == signal.shape
    assert np.all(np.isfinite(out))


def test_vad_gate_silences_quiet_region():
    sr = 16000
    loud = _tone(300, 0.5, sr)
    quiet = (0.0005 * np.ones(sr // 2)).astype(np.float32)
    signal = np.concatenate([loud, quiet])
    out = vad_gate(signal, sr)
    # quiet tail (well past the boundary frame) should be zeroed; loud head preserved
    assert np.max(np.abs(out[sr // 2 + 480 :])) == 0.0
    assert np.max(np.abs(out[: sr // 2])) > 0.1


def test_preprocess_pcm16_noop_when_disabled():
    pcm = (np.zeros(1000, dtype="<i2")).tobytes()
    assert preprocess_pcm16(pcm, 16000, denoise=False, vad=False) == pcm


def test_preprocess_wav_roundtrips_mono16():
    wav = float_array_to_wav_bytes(_tone(440, 1.0, 16000), 16000)
    out = preprocess_wav_bytes(wav, denoise=True, vad=True)
    assert out[:4] == b"RIFF"
    assert abs(wav_duration_seconds(out) - 1.0) < 1e-2


def test_preprocess_wav_passthrough_for_non_wav():
    junk = b"not a wav file"
    assert preprocess_wav_bytes(junk, denoise=True, vad=True) == junk


def test_preprocess_wav_passthrough_for_stereo():
    # stereo (2ch) is not mono16 -> passthrough unchanged
    stereo = pcm16_to_wav_bytes(b"\x00\x00\x00\x00" * 100, sample_rate=16000, channels=2)
    assert preprocess_wav_bytes(stereo, denoise=True, vad=True) == stereo
