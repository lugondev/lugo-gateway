"""Voice Activity Detection backends.

Three selectable backends:
- ``energy``   — dependency-free numpy energy gate (always available).
- ``silero``   — Silero VAD (needs ``torch`` + ``silero-vad``).
- ``pyannote`` — pyannote.audio VAD (needs ``torch`` + ``pyannote.audio``; some
  models require a Hugging Face token via ``PYANNOTE_AUTH_TOKEN``).

All backends return a length-preserving array with non-speech regions zeroed, so
downstream decoders stay time-aligned. If a selected backend is unavailable (its
library isn't installed) the call falls back to the energy gate.
"""

import importlib.util
import logging

import numpy as np

from app.core.audio import vad_gate
from app.core.settings import settings

logger = logging.getLogger(__name__)

VAD_BACKENDS = ("energy", "silero", "pyannote")

_silero_cache: dict[str, object] = {}
_pyannote_cache: dict[str, object] = {}


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:
        return False


def available_backends() -> dict[str, bool]:
    torch_ok = _installed("torch")
    return {
        "energy": True,
        "silero": torch_ok and _installed("silero_vad"),
        "pyannote": torch_ok and _installed("pyannote.audio"),
    }


def _apply_mask(samples: np.ndarray, regions: list[tuple[int, int]]) -> np.ndarray:
    # No detected speech -> pass through unchanged (avoid wiping the whole clip).
    if not regions:
        return samples
    out = np.zeros_like(samples)
    n = len(samples)
    for start, end in regions:
        s, e = max(0, int(start)), min(n, int(end))
        if e > s:
            out[s:e] = samples[s:e]
    return out


def _silero_regions(samples: np.ndarray, sample_rate: int) -> list[tuple[int, int]]:
    import torch
    from silero_vad import get_speech_timestamps, load_silero_vad

    if "model" not in _silero_cache:
        _silero_cache["model"] = load_silero_vad()
    audio = torch.from_numpy(np.asarray(samples, dtype=np.float32))
    ts = get_speech_timestamps(audio, _silero_cache["model"], sampling_rate=sample_rate)
    return [(t["start"], t["end"]) for t in ts]


def _pyannote_regions(samples: np.ndarray, sample_rate: int) -> list[tuple[int, int]]:
    import torch
    from pyannote.audio import Pipeline

    if "pipeline" not in _pyannote_cache:
        token = settings.pyannote_auth_token or True
        _pyannote_cache["pipeline"] = Pipeline.from_pretrained(
            settings.pyannote_vad_model, use_auth_token=token
        )
    waveform = torch.from_numpy(np.asarray(samples, dtype=np.float32)).unsqueeze(0)
    annotation = _pyannote_cache["pipeline"]({"waveform": waveform, "sample_rate": sample_rate})
    return [
        (int(seg.start * sample_rate), int(seg.end * sample_rate))
        for seg in annotation.get_timeline().support()
    ]


def apply_vad(samples: np.ndarray, sample_rate: int, backend: str = "energy") -> np.ndarray:
    """Gate non-speech using the chosen backend, falling back to energy if unavailable."""
    samples = np.asarray(samples, dtype=np.float32).reshape(-1)
    if samples.size == 0:
        return samples

    avail = available_backends()
    try:
        if backend == "silero" and avail["silero"]:
            if sample_rate not in (8000, 16000):
                logger.warning("Silero VAD needs 8k/16k audio; got %s — using energy", sample_rate)
                return vad_gate(samples, sample_rate)
            return _apply_mask(samples, _silero_regions(samples, sample_rate))
        if backend == "pyannote" and avail["pyannote"]:
            return _apply_mask(samples, _pyannote_regions(samples, sample_rate))
    except Exception as exc:  # noqa: BLE001 - never break transcription on VAD error
        logger.warning("VAD backend %s failed (%s); using energy gate", backend, exc)
        return vad_gate(samples, sample_rate)

    if backend in ("silero", "pyannote"):
        logger.info("VAD backend %s unavailable; using energy gate", backend)
    return vad_gate(samples, sample_rate)
