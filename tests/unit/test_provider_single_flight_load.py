"""Two threads racing to build a provider's model must only build it once.

Regression coverage for the cold-start bug where a background warm() task and the
first real turn's transcribe/synthesize call raced on different threads, each
independently building (and caching) the model — doubling first-turn latency.
"""

import threading
import time

import pytest


@pytest.fixture(autouse=True)
def _reset_caches():
    from app.services.stt.providers import whisper_provider
    from app.services.tts.providers import vieneu_provider

    whisper_provider._MODEL_CACHE.clear()
    vieneu_provider._CACHE.clear()
    yield
    whisper_provider._MODEL_CACHE.clear()
    vieneu_provider._CACHE.clear()


def _race(build_once, n_threads: int = 8) -> int:
    barrier = threading.Barrier(n_threads)

    def worker():
        barrier.wait()
        build_once()

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    return 0


def test_whisper_provider_builds_model_once_under_race(monkeypatch):
    import faster_whisper
    from app.services.stt.providers import whisper_provider

    calls = []

    class FakeModel:
        def __init__(self, *a, **kw):
            calls.append(1)
            time.sleep(0.05)  # widen the race window

    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModel)
    monkeypatch.setattr(whisper_provider, "resolve_whisper_model", lambda m: m)

    provider = whisper_provider.WhisperProvider()
    _race(provider._load_model)

    assert len(calls) == 1


def test_vieneu_provider_builds_model_once_under_race(monkeypatch):
    import vieneu
    from app.services.tts.providers import vieneu_provider

    calls = []

    class FakeVieneu:
        def __init__(self, *a, **kw):
            calls.append(1)
            time.sleep(0.05)

    monkeypatch.setattr(vieneu, "Vieneu", FakeVieneu)

    provider = vieneu_provider.VieNeuProvider()
    _race(provider._model)

    assert len(calls) == 1
