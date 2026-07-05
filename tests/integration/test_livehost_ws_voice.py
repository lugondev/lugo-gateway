import asyncio

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
    name = "stub-livehost"

    async def transcribe_bytes(self, audio_bytes, language=None) -> STTResult:
        return STTResult(engine=self.name, text="chao ban", is_final=True)


class _FailingSTT(STTProvider):
    name = "stub-livehost-failing"

    async def transcribe_bytes(self, audio_bytes, language=None) -> STTResult:
        raise RuntimeError("boom")


class _StubTTS(TTSProvider):
    name = "stub-livehost-tts"

    async def synthesize(self, payload) -> TTSResult:
        return TTSResult(
            engine=self.name, sample_rate=24000, audio_url="/artifacts/x.wav",
            duration_seconds=0.1, text=payload.text, mock=True,
        )


@pytest.fixture(autouse=True)
def _register_stub(monkeypatch):
    monkeypatch.setattr(settings, "enable_mock_engines", True)
    monkeypatch.setattr(settings, "conversation_llm_base_url", "")
    stt_service.providers["stub-livehost"] = _StubSTT()
    stt_service.providers["stub-livehost-failing"] = _FailingSTT()
    tts_service.providers["stub-livehost-tts"] = _StubTTS()
    yield
    stt_service.providers.pop("stub-livehost", None)
    stt_service.providers.pop("stub-livehost-failing", None)
    tts_service.providers.pop("stub-livehost-tts", None)


def _loud(ms: int) -> bytes:
    n = int(SR * ms / 1000)
    return (np.full(n, 0.2, dtype=np.float32) * 32767).astype("<i2").tobytes()


def _silence(ms: int) -> bytes:
    return (b"\x00\x00") * int(SR * ms / 1000)


def test_livehost_voice_turn_end_to_end():
    client = TestClient(app)
    url = "/v1/livehost/stream?stt_engine=stub-livehost&tts_engine=stub-livehost-tts&sample_rate=16000"
    with client.websocket_connect(url) as ws:
        started = ws.receive_json()
        assert started["event"] == "session_started"
        session_id = started["session_id"]

        ws.send_bytes(_loud(500))
        ws.send_bytes(_silence(500))
        ws.send_bytes(_silence(500))

        events = []
        for _ in range(20):
            ev = ws.receive_json()
            events.append(ev)
            if ev["event"] == "turn_done":
                break

        kinds = [e["event"] for e in events]
        assert "user_transcript" in kinds
        assert "audio_chunk" in kinds
        assert kinds[-1] == "turn_done"

    from app.services.livehost.registry import livehost_registry
    assert livehost_registry.get(session_id) is None  # cleaned up on disconnect


def test_livehost_voice_turn_stt_failure_still_sends_turn_done():
    client = TestClient(app)
    url = "/v1/livehost/stream?stt_engine=stub-livehost-failing&tts_engine=stub-livehost-tts&sample_rate=16000"
    with client.websocket_connect(url) as ws:
        started = ws.receive_json()
        assert started["event"] == "session_started"

        ws.send_bytes(_loud(500))
        ws.send_bytes(_silence(500))
        ws.send_bytes(_silence(500))

        events = []
        for _ in range(20):
            ev = ws.receive_json()
            events.append(ev)
            if ev["event"] == "turn_done":
                break

        kinds = [e["event"] for e in events]
        assert "error" in kinds
        assert "turn_done" in kinds
        # the client must not be left hanging: error must arrive before turn_done
        assert kinds.index("error") < kinds.index("turn_done")
