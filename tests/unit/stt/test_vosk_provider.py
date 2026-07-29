"""Vosk decode must run off the event loop.

Vosk (Kaldi) decoding is pure CPU work; running it inline in the provider's
`async def` freezes every other WS session and SSE stream for the duration of
the decode (seconds on long clips, plus a multi-second model load on first
call). These tests fake the `vosk` module and record which thread each decode
call runs on: it must never be the event-loop thread.
"""

import asyncio
import json
import sys
import threading
import time
import types

import pytest

from app.core.audio import pcm16_to_wav_bytes


_MODEL_CONSTRUCTIONS: list[str] = []


class _FakeModel:
    def __init__(self, path: str) -> None:
        # Simulate the real multi-second Kaldi load: long enough that two
        # concurrent cold-cache callers overlap inside __init__ unless a lock
        # serializes them.
        _MODEL_CONSTRUCTIONS.append(path)
        time.sleep(0.05)
        self.path = path


@pytest.fixture
def model_constructions():
    _MODEL_CONSTRUCTIONS.clear()
    return _MODEL_CONSTRUCTIONS


@pytest.fixture
def decode_threads(monkeypatch, tmp_path):
    """Install a fake `vosk` module whose recognizer records the thread ident
    of every decode call, and point the provider at an existing tmp model dir."""
    threads: list[int] = []

    class _FakeKaldiRecognizer:
        def __init__(self, model, sample_rate) -> None:
            pass

        def AcceptWaveform(self, pcm: bytes) -> bool:
            threads.append(threading.get_ident())
            return False

        def PartialResult(self) -> str:
            threads.append(threading.get_ident())
            return json.dumps({"partial": "xin"})

        def Result(self) -> str:
            threads.append(threading.get_ident())
            return json.dumps({"text": ""})

        def FinalResult(self) -> str:
            threads.append(threading.get_ident())
            return json.dumps({"text": "xin chao"})

    fake_vosk = types.ModuleType("vosk")
    fake_vosk.Model = _FakeModel
    fake_vosk.KaldiRecognizer = _FakeKaldiRecognizer
    monkeypatch.setitem(sys.modules, "vosk", fake_vosk)

    from app.services.stt.providers import vosk_provider

    monkeypatch.setattr(vosk_provider, "_MODEL_CACHE", {})
    monkeypatch.setattr(vosk_provider, "_active_path", str(tmp_path))
    return threads


async def test_transcribe_bytes_decodes_off_the_event_loop(decode_threads):
    from app.services.stt.providers.vosk_provider import VoskProvider

    wav = pcm16_to_wav_bytes(b"\x00\x00" * 16000, sample_rate=16000)
    result = await VoskProvider().transcribe_bytes(wav)

    assert result.text == "xin chao"
    loop_thread = threading.get_ident()
    assert decode_threads, "fake recognizer was never called"
    assert all(t != loop_thread for t in decode_threads)


async def test_stream_accept_and_finalize_decode_off_the_event_loop(decode_threads):
    from app.services.stt.providers.vosk_provider import VoskStream

    stream = VoskStream("vosk", 16000)
    partials = await stream.accept(b"\x00\x00" * 320)
    final = await stream.finalize()

    assert partials and partials[0].text == "xin"
    assert final is not None and final.text == "xin chao"
    loop_thread = threading.get_ident()
    assert all(t != loop_thread for t in decode_threads)


async def test_concurrent_cold_transcribes_load_the_model_once(decode_threads, model_constructions):
    """_load_vosk_model's check-then-insert used to be safe only because every
    caller ran on the event-loop thread; now that decode runs on worker
    threads, two concurrent cold-cache requests must not BOTH construct the
    multi-hundred-MB Model (transient double RAM -> OOM risk on the RPi)."""
    from app.services.stt.providers.vosk_provider import VoskProvider

    wav = pcm16_to_wav_bytes(b"\x00\x00" * 1600, sample_rate=16000)
    provider = VoskProvider()
    await asyncio.gather(provider.transcribe_bytes(wav), provider.transcribe_bytes(wav))

    assert len(model_constructions) == 1


async def test_open_stream_defers_the_model_load_off_the_event_loop(decode_threads, model_constructions):
    """open_stream() is called synchronously on the event loop by the WS
    handler (routes/stt.py); vosk has no warm(), so the first cold-cache
    stream used to freeze the loop for the whole model load in __init__.
    The recognizer must be built lazily, on the first (off-loop) accept()."""
    from app.services.stt.providers.vosk_provider import VoskProvider

    stream = VoskProvider().open_stream(16000)
    assert model_constructions == []  # nothing loaded on the loop thread

    partials = await stream.accept(b"\x00\x00" * 320)
    assert partials and partials[0].text == "xin"
    assert len(model_constructions) == 1


async def test_finalize_without_any_audio_does_not_load_the_model(decode_threads, model_constructions):
    from app.services.stt.providers.vosk_provider import VoskProvider

    stream = VoskProvider().open_stream(16000)
    assert await stream.finalize() is None
    assert model_constructions == []


async def test_stream_decode_runs_on_the_dedicated_stream_executor(decode_threads, monkeypatch):
    """Per-frame decodes (16-50/s, single-digit ms, latency-sensitive) must
    not share the default to_thread pool with 100-300ms PBKDF2 hashes and
    whole-utterance decodes, or partials stutter whenever the pool is busy."""
    names: list[str] = []
    from app.services.stt.providers import vosk_provider

    stream = vosk_provider.VoskProvider().open_stream(16000)

    real_accept_sync = stream._accept_sync

    def spy_accept_sync(pcm):
        names.append(threading.current_thread().name)
        return real_accept_sync(pcm)

    monkeypatch.setattr(stream, "_accept_sync", spy_accept_sync)
    await stream.accept(b"\x00\x00" * 320)

    assert names and all(n.startswith("vosk-stream") for n in names)
