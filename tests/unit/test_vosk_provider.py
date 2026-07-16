"""Vosk decode must run off the event loop.

Vosk (Kaldi) decoding is pure CPU work; running it inline in the provider's
`async def` freezes every other WS session and SSE stream for the duration of
the decode (seconds on long clips, plus a multi-second model load on first
call). These tests fake the `vosk` module and record which thread each decode
call runs on: it must never be the event-loop thread.
"""

import json
import sys
import threading
import types

import pytest

from app.core.audio import pcm16_to_wav_bytes


class _FakeModel:
    def __init__(self, path: str) -> None:
        self.path = path


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
