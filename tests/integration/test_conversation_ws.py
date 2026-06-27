import asyncio
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.schemas.stt import STTResult
from app.schemas.tts import TTSResult
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service

SR = 16000


class _StubSTT(STTProvider):
    name = "stub-conv"

    async def transcribe_bytes(self, audio_bytes, language=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chào trợ lý", is_final=True)


class _SlowTTS(TTSProvider):
    name = "slow-conv-tts"

    async def synthesize(self, payload) -> TTSResult:
        await asyncio.sleep(0.5)  # window for barge-in
        return TTSResult(
            engine=self.name, sample_rate=24000, audio_url="/artifacts/x.wav",
            duration_seconds=0.1, text=payload.text, mock=True,
        )


@pytest.fixture(autouse=True)
def _register_stub(monkeypatch):
    # Keep the test hermetic regardless of .env: mock TTS + built-in echo responder
    # (no external Ollama / real model calls).
    monkeypatch.setattr(settings, "enable_mock_engines", True)
    monkeypatch.setattr(settings, "conversation_llm_base_url", "")
    stt_service.providers["stub-conv"] = _StubSTT()
    tts_service.providers["slow-conv-tts"] = _SlowTTS()
    yield
    stt_service.providers.pop("stub-conv", None)
    tts_service.providers.pop("slow-conv-tts", None)


def _loud(ms: int) -> bytes:
    n = int(SR * ms / 1000)
    return (np.full(n, 0.2, dtype=np.float32) * 32767).astype("<i2").tobytes()


def _silence(ms: int) -> bytes:
    return (b"\x00\x00") * int(SR * ms / 1000)


def test_conversation_turn_end_to_end():
    client = TestClient(app)
    url = "/v1/conversation/stream?stt_engine=stub-conv&tts_engine=omnivoice&sample_rate=16000"
    with client.websocket_connect(url) as ws:
        assert ws.receive_json()["event"] == "session_started"

        ws.send_bytes(_loud(500))
        assert ws.receive_json()["event"] == "speech_start"
        ws.send_bytes(_loud(400))
        ws.send_bytes(_silence(500))
        ws.send_bytes(_silence(500))  # crosses 700ms silence -> endpoint

        events = []
        audio_frames = 0
        for _ in range(40):
            msg = ws.receive()
            if msg.get("bytes") is not None:  # inline WAV audio frame
                audio_frames += 1
                continue
            ev = json.loads(msg["text"])
            events.append(ev)
            if ev["event"] == "turn_done":
                break

        types = [e["event"] for e in events]
        assert "speech_end" in types
        assert "user_transcript" in types
        assert "response_text" in types
        assert "audio_chunk" in types
        assert types[-1] == "turn_done"
        assert audio_frames >= 1  # audio delivered as binary frames

        transcript = next(e for e in events if e["event"] == "user_transcript")
        assert transcript["text"] == "xin chào trợ lý"


def test_conversation_barge_in_aborts_turn():
    client = TestClient(app)
    url = "/v1/conversation/stream?stt_engine=stub-conv&tts_engine=slow-conv-tts&sample_rate=16000"
    with client.websocket_connect(url) as ws:
        assert ws.receive_json()["event"] == "session_started"
        ws.send_bytes(_loud(500))
        assert ws.receive_json()["event"] == "speech_start"
        ws.send_bytes(_loud(400))
        ws.send_bytes(_silence(500))
        ws.send_bytes(_silence(500))  # endpoint -> turn starts (slow TTS)
        ws.send_bytes(_loud(500))  # barge-in while assistant is synthesizing

        seen = []
        for _ in range(12):
            ev = ws.receive_json()["event"]
            seen.append(ev)
            if ev == "aborted":
                break
        assert "aborted" in seen  # the in-progress turn was cancelled


def test_conversation_unknown_engine_errors():
    client = TestClient(app)
    with client.websocket_connect("/v1/conversation/stream?stt_engine=nope") as ws:
        assert ws.receive_json()["event"] == "error"
