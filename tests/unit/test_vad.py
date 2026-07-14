import numpy as np

from app.services.vad import VAD_BACKENDS, apply_vad, available_backends


def _signal(sr=16000):
    loud = (0.5 * np.sin(2 * np.pi * 300 * np.arange(sr // 2) / sr)).astype(np.float32)
    quiet = (0.0005 * np.ones(sr // 2)).astype(np.float32)
    return np.concatenate([loud, quiet])


def test_available_backends_energy_always_true():
    avail = available_backends()
    assert avail["energy"] is True
    assert set(avail) == set(VAD_BACKENDS)


def test_apply_vad_energy_gates_quiet_tail():
    sr = 16000
    out = apply_vad(_signal(sr), sr, backend="energy")
    assert np.max(np.abs(out[sr // 2 + 480 :])) == 0.0


def test_unavailable_backend_falls_back_to_energy():
    sr = 16000
    # silero/pyannote not installed in this env -> must fall back, not crash
    out = apply_vad(_signal(sr), sr, backend="silero")
    assert out.shape == (sr,)
    assert np.all(np.isfinite(out))


def test_empty_input():
    assert apply_vad(np.zeros(0, dtype=np.float32), 16000, backend="pyannote").size == 0


def test_clear_pyannote_cache_empties_the_cache():
    from app.services import vad as mod

    mod._pyannote_cache["pipeline"] = object()
    mod.clear_pyannote_cache()
    assert mod._pyannote_cache == {}
