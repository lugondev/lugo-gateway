import numpy as np

from app.core.audio import (
    float_array_to_wav_bytes,
    pcm16_to_float_array,
    pcm16_to_wav_bytes,
    read_wav,
    silent_wav_bytes,
    wav_bytes_to_pcm16,
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


# ---------------------------------------------------------- wav_bytes_to_pcm16

def _tone_pcm16(n: int, freq: float, sr: int) -> bytes:
    t = np.arange(n) / sr
    samples = (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return (samples * 32767).astype("<i2").tobytes()


def _tone_mp3(n: int, freq: float, sr: int) -> bytes:
    import io

    import soundfile as sf

    pcm = _tone_pcm16(n, freq, sr)
    float_samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    buffer = io.BytesIO()
    sf.write(buffer, float_samples, sr, format="MP3")
    return buffer.getvalue()


def test_wav_bytes_to_pcm16_decodes_mp3_via_soundfile_fallback():
    # Not a RIFF/WAVE container -- wave.open() raises, and wav_bytes_to_pcm16
    # falls back to soundfile/libsndfile, which decodes mp3 (and ogg/flac)
    # directly. This is what lets an mp3 uploaded through the admin STT test
    # page just work instead of needing a clear rejection or manual conversion.
    mp3_bytes = _tone_mp3(1600, 220.0, 16000)  # 100ms @ 16kHz
    assert mp3_bytes[:4] != b"RIFF"  # sanity: genuinely not a WAV container

    pcm = wav_bytes_to_pcm16(mp3_bytes, target_sr=16000)
    # Lossy round-trip through mp3 encoding, so this isn't sample-exact -- just
    # assert it decoded to roughly the right length and isn't silence/garbage.
    assert abs(len(pcm) // 2 - 1600) <= 200
    out_arr = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    assert out_arr.std() > 1000  # a 220Hz tone, not near-zero noise


def test_wav_duration_seconds_handles_mp3():
    # /v1/stt/transcribe reports duration from the *original* upload bytes
    # after a provider has already decoded its own copy -- this used to be an
    # uncaught wave.Error (plain-text 500, not JSON) for any mp3/ogg/flac
    # upload that got far enough to actually transcribe successfully.
    mp3_bytes = _tone_mp3(16000, 220.0, 16000)  # 1s @ 16kHz
    assert abs(wav_duration_seconds(mp3_bytes) - 1.0) < 0.05


def test_read_wav_handles_mp3():
    mp3_bytes = _tone_mp3(1600, 220.0, 16000)  # 100ms @ 16kHz
    frames, rate, channels, width = read_wav(mp3_bytes)
    assert rate == 16000
    assert channels == 1
    assert width == 2
    assert abs(len(frames) // (width * channels) - 1600) <= 200


def test_wav_bytes_to_pcm16_raises_libsndfile_error_for_garbage():
    import soundfile as sf

    try:
        wav_bytes_to_pcm16(b"not audio at all", target_sr=16000)
        raise AssertionError("expected LibsndfileError for undecodable bytes")
    except sf.LibsndfileError:
        pass


def test_wav_bytes_to_pcm16_rejects_non_pcm16_width():
    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(1)  # 8-bit -- unsupported
        wav_file.setframerate(16000)
        wav_file.writeframes(b"\x00" * 100)

    try:
        wav_bytes_to_pcm16(buffer.getvalue(), target_sr=16000)
        raise AssertionError("expected ValueError for unsupported sample width")
    except ValueError as exc:
        assert "unsupported sample width" in str(exc)
