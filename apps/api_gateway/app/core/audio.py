"""Audio helpers shared across STT/TTS.

The system-wide audio contract for streaming is PCM signed 16-bit, mono.
Sample rate is configurable (default 16 kHz for STT, 24 kHz for OmniVoice TTS).
"""

import io
import wave

import numpy as np
import soundfile as sf

# Raised by the WAV-first/soundfile-fallback readers below (wav_duration_seconds,
# read_wav, wav_bytes_to_pcm16) when bytes are genuinely unparseable by either --
# not just "not a WAV container" (mp3/ogg/flac all decode fine via soundfile).
_UNDECODABLE_AUDIO_ERRORS = (wave.Error, sf.LibsndfileError)


def pcm16_to_wav_bytes(pcm: bytes, sample_rate: int, channels: int = 1) -> bytes:
    """Wrap raw PCM signed-16 bytes in a WAV container."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)
    return buffer.getvalue()


def float_array_to_wav_bytes(samples: np.ndarray, sample_rate: int) -> bytes:
    """Convert a float32/float64 waveform in [-1, 1] to mono PCM16 WAV bytes."""
    samples = np.asarray(samples, dtype=np.float32).reshape(-1)
    clipped = np.clip(samples, -1.0, 1.0)
    pcm16 = (clipped * 32767.0).astype("<i2").tobytes()
    return pcm16_to_wav_bytes(pcm16, sample_rate=sample_rate, channels=1)


def pcm16_to_float_array(pcm: bytes) -> np.ndarray:
    """Convert raw PCM signed-16 bytes to a float32 waveform in [-1, 1]."""
    if not pcm:
        return np.zeros(0, dtype=np.float32)
    int16 = np.frombuffer(pcm, dtype="<i2")
    return (int16.astype(np.float32)) / 32768.0


def wav_duration_seconds(wav_bytes: bytes) -> float:
    """Return the duration in seconds of an audio byte payload.

    Same WAV-first, soundfile-fallback shape as wav_bytes_to_pcm16 above --
    needed because /v1/stt/transcribe calls this on the *original* upload
    bytes (mp3/ogg/flac included) to report duration in the response, after
    a provider has already decoded its own private copy for transcription."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
            frames = wav_file.getnframes()
            rate = wav_file.getframerate()
        return frames / float(rate) if rate else 0.0
    except wave.Error:
        return sf.info(io.BytesIO(wav_bytes)).duration


def silent_wav_bytes(duration_seconds: float, sample_rate: int) -> bytes:
    """Generate a mono PCM16 WAV of silence for the given duration."""
    n_samples = max(1, int(duration_seconds * sample_rate))
    pcm = (b"\x00\x00") * n_samples
    return pcm16_to_wav_bytes(pcm, sample_rate=sample_rate, channels=1)


def read_wav(wav_bytes: bytes) -> tuple[bytes, int, int, int]:
    """Return (pcm_frames, sample_rate, channels, sample_width) of an audio payload.

    Same WAV-first, soundfile-fallback shape as wav_bytes_to_pcm16 above -- the
    segmented-transcribe path (stt.py's use_segment branch) calls this on the
    raw upload, mp3/ogg/flac included."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
            frames = wav_file.readframes(wav_file.getnframes())
            return frames, wav_file.getframerate(), wav_file.getnchannels(), wav_file.getsampwidth()
    except wave.Error:
        samples, rate = sf.read(io.BytesIO(wav_bytes), dtype="int16", always_2d=True)
        channels = samples.shape[1]
        return samples.tobytes(), rate, channels, 2


def wav_bytes_to_pcm16(wav_bytes: bytes, target_sr: int) -> bytes:
    """Decode an audio payload, downmix to mono, resample to target_sr, return PCM16 bytes.

    WAV is read directly (no extra dependency for the common case); anything
    else that isn't a RIFF/WAVE container (mp3, ogg, flac, ...) falls back to
    soundfile/libsndfile, which decodes all of those already -- this is what
    lets e.g. an mp3 uploaded through the admin STT test page just work instead
    of needing a clear rejection or a manual conversion step. Raises
    ``soundfile.LibsndfileError`` for bytes neither reader can make sense of."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
            sr = wav_file.getframerate()
            ch = wav_file.getnchannels()
            width = wav_file.getsampwidth()
            raw = wav_file.readframes(wav_file.getnframes())
        if width != 2:  # only PCM16 inputs expected from our TTS engines
            raise ValueError(f"unsupported sample width: {width}")
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        if ch > 1:
            samples = samples.reshape(-1, ch).mean(axis=1)
    except wave.Error:
        samples, sr = sf.read(io.BytesIO(wav_bytes), dtype="float32", always_2d=True)
        samples = samples.mean(axis=1) if samples.shape[1] > 1 else samples[:, 0]
    if sr != target_sr:
        from math import gcd

        from scipy.signal import resample_poly

        g = gcd(sr, target_sr)
        samples = resample_poly(samples, target_sr // g, sr // g)
    return (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()


# ---------------------------------------------------------------- preprocessing

def reduce_noise(
    samples: np.ndarray,
    amount: float = 0.85,
    frame: int = 1024,
    hop: int = 256,
) -> np.ndarray:
    """Spectral-subtraction noise reduction (overlap-add STFT).

    Estimates a per-frequency noise floor from the quietest frames and subtracts
    a fraction (``amount``) of it. Dependency-free (numpy only).
    """
    samples = np.asarray(samples, dtype=np.float32).reshape(-1)
    if samples.size < frame:
        return samples

    window = np.hanning(frame).astype(np.float32)
    n_frames = 1 + (len(samples) - frame) // hop
    idx = np.arange(frame)[None, :] + hop * np.arange(n_frames)[:, None]
    frames = samples[idx] * window

    spec = np.fft.rfft(frames, axis=1)
    mag = np.abs(spec)
    phase = np.angle(spec)

    noise = np.percentile(mag, 10, axis=0)  # quiet-frame estimate per frequency
    reduced = np.maximum(mag - amount * noise[None, :], 0.0)
    new_spec = reduced * np.exp(1j * phase)
    rec = np.fft.irfft(new_spec, n=frame, axis=1).astype(np.float32) * window

    out = np.zeros(len(samples), dtype=np.float32)
    wsum = np.zeros(len(samples), dtype=np.float32)
    win_sq = window ** 2
    for i in range(n_frames):
        start = i * hop
        out[start : start + frame] += rec[i]
        wsum[start : start + frame] += win_sq
    nonzero = wsum > 1e-6
    out[nonzero] /= wsum[nonzero]
    return out


def vad_gate(
    samples: np.ndarray,
    sample_rate: int,
    threshold_db: float = -40.0,
    frame_ms: int = 30,
) -> np.ndarray:
    """Energy-based VAD: zero out frames quieter than ``threshold_db`` below peak.

    Preserves length and timing (decoders stay aligned); silence/background between
    speech is muted.
    """
    samples = np.asarray(samples, dtype=np.float32).reshape(-1)
    if samples.size == 0:
        return samples

    frame = max(1, int(sample_rate * frame_ms / 1000))
    peak = float(np.max(np.abs(samples))) or 1.0
    threshold = peak * (10.0 ** (threshold_db / 20.0))

    out = samples.copy()
    for start in range(0, len(samples), frame):
        seg = out[start : start + frame]
        rms = float(np.sqrt(np.mean(seg ** 2))) if seg.size else 0.0
        if rms < threshold:
            out[start : start + frame] = 0.0
    return out


def preprocess_pcm16(
    pcm: bytes,
    sample_rate: int,
    denoise: bool = False,
    vad: bool = False,
    amount: float = 0.85,
    vad_fn=None,
) -> bytes:
    """Apply optional noise reduction / VAD gating to raw PCM16 mono, return PCM16.

    ``vad_fn(samples, sample_rate) -> samples`` selects the VAD backend; defaults to
    the built-in energy gate (keeps this module dependency-free).
    """
    if not (denoise or vad) or not pcm:
        return pcm
    samples = pcm16_to_float_array(pcm)
    if denoise:
        samples = reduce_noise(samples, amount=amount)
    if vad:
        samples = (vad_fn or vad_gate)(samples, sample_rate)
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def preprocess_wav_bytes(
    wav_bytes: bytes,
    denoise: bool = False,
    vad: bool = False,
    amount: float = 0.85,
    vad_fn=None,
) -> bytes:
    """Apply noise reduction / VAD to a WAV payload (mono PCM16 only); pass-through else."""
    if not (denoise or vad):
        return wav_bytes
    try:
        pcm, rate, channels, width = read_wav(wav_bytes)
    except _UNDECODABLE_AUDIO_ERRORS:
        # read_wav already falls back from wave to soundfile (mp3/ogg/flac all
        # decode fine there) -- reaching here means genuinely unparseable bytes,
        # not just "not a WAV container". Leave those for the engine to reject.
        return wav_bytes
    if channels != 1 or width != 2:
        return wav_bytes
    processed = preprocess_pcm16(
        pcm, rate, denoise=denoise, vad=vad, amount=amount, vad_fn=vad_fn
    )
    return pcm16_to_wav_bytes(processed, sample_rate=rate, channels=1)
