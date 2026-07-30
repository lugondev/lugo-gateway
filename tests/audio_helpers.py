"""Shared audio-fixture helpers for tests that need a non-WAV container.

Lives at the tests/ top level (alongside conftest.py) rather than inside a
specific test module: tests/conftest.py is guaranteed to load before any test
module executes (pytest loads all applicable conftest.py files up front), and
since tests/ has no __init__.py, loading it inserts tests/ onto sys.path --
which is what makes ``from audio_helpers import ...`` resolve from any test
module in the tree, regardless of collection order.
"""

import io

import numpy as np
import soundfile as sf


def _tone_pcm16(n: int, freq: float, sr: int) -> bytes:
    t = np.arange(n) / sr
    samples = (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return (samples * 32767).astype("<i2").tobytes()


def _tone_mp3(n: int, freq: float, sr: int) -> bytes:
    pcm = _tone_pcm16(n, freq, sr)
    float_samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    buffer = io.BytesIO()
    sf.write(buffer, float_samples, sr, format="MP3")
    return buffer.getvalue()
