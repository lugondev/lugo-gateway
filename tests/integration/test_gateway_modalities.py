"""Unified gateway over /v1/conversation/stream: text/audio in -> text/audio out."""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.schemas.stt import STTResult
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service

SR = 16000


class _StubSTT(STTProvider):
    name = "stub-gw"

    async def transcribe_bytes(self, audio_bytes, language=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chào", is_final=True)


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    monkeypatch.setattr(settings, "enable_mock_engines", True)
    monkeypatch.setattr(settings, "conversation_llm_base_url", "")  # echo responder
    stt_service.providers["stub-gw"] = _StubSTT()
    yield
    stt_service.providers.pop("stub-gw", None)


def _loud(ms):
    n = int(SR * ms / 1000)
    return (np.full(n, 0.2, dtype=np.float32) * 32767).astype("<i2").tobytes()


def _drain(ws, stop="turn_done", n=40):
    evs = []
    for _ in range(n):
        e = ws.receive_json()
        evs.append(e)
        if e["event"] == stop:
            break
    return evs


def test_text_to_text():
    c = TestClient(app)
    with c.websocket_connect("/v1/conversation/stream?stt_engine=stub-gw&tts_engine=omnivoice&output=text") as ws:
        started = ws.receive_json()
        assert started["event"] == "session_started" and started["output"] == ["text"]
        ws.send_json({"type": "text", "text": "xin chào"})
        evs = _drain(ws)
        types = [e["event"] for e in evs]
        assert "user_transcript" in types and "response_text" in types
        assert "audio_chunk" not in types  # text-only -> no TTS
        assert next(e for e in evs if e["event"] == "user_transcript")["text"] == "xin chào"


def test_text_to_audio_url():
    c = TestClient(app)
    with c.websocket_connect("/v1/conversation/stream?stt_engine=stub-gw&tts_engine=omnivoice&output=audio") as ws:
        assert ws.receive_json()["event"] == "session_started"
        ws.send_json({"type": "text", "text": "xin chào"})
        evs = _drain(ws)
        types = [e["event"] for e in evs]
        assert "audio_chunk" in types
        assert "response_text" not in types  # audio-only -> no text events


def _opus_ok():
    try:
        import opuslib

        opuslib.Encoder(24000, 1, opuslib.APPLICATION_VOIP)
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _opus_ok(), reason="libopus not loadable")
def test_text_to_opus_frames(monkeypatch):
    # Verify Opus framing only; disable real-time pacing so the test doesn't sleep
    # through the (multi-second) mock reply audio. Pacing schedule is unit-tested.
    from app.core.settings import settings

    monkeypatch.setattr(settings, "conversation_opus_pace", False)
    c = TestClient(app)
    url = "/v1/conversation/stream?stt_engine=stub-gw&tts_engine=omnivoice&output=audio&audio_out=opus&output_sample_rate=24000"
    with c.websocket_connect(url) as ws:
        started = ws.receive_json()
        assert started["audio_out"] == "opus" and started["output_sample_rate"] == 24000
        ws.send_json({"type": "text", "text": "xin chào"})
        saw_start = saw_end = False
        binary_frames = 0
        for _ in range(200):
            msg = ws.receive()
            if "bytes" in msg and msg["bytes"] is not None:
                binary_frames += 1
                continue
            import json

            ev = json.loads(msg["text"])["event"]
            if ev == "audio_start":
                saw_start = True
            elif ev == "audio_end":
                saw_end = True
            elif ev == "turn_done":
                break
        assert saw_start and saw_end and binary_frames > 0


def test_audio_to_text():
    c = TestClient(app)
    url = "/v1/conversation/stream?stt_engine=stub-gw&tts_engine=omnivoice&output=text&sample_rate=16000"
    with c.websocket_connect(url) as ws:
        assert ws.receive_json()["event"] == "session_started"
        ws.send_bytes(_loud(500))
        assert ws.receive_json()["event"] == "speech_start"
        ws.send_bytes(_loud(400))
        ws.send_bytes((b"\x00\x00") * int(SR * 1.0))  # silence -> endpoint
        evs = _drain(ws)
        types = [e["event"] for e in evs]
        assert "user_transcript" in types and "response_text" in types
        assert "audio_chunk" not in types
