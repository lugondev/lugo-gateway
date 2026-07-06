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
            duration_seconds=0.1, text=payload.text,
        )


@pytest.fixture(autouse=True)
def _register_stub(monkeypatch):
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


def test_livehost_session_started_send_failure_does_not_leak_registry(monkeypatch):
    # Regression test: if the very first websocket.send_json (the "session_started"
    # event) raises -- e.g. because the client already disconnected -- the finally
    # block must still run ingestor.stop()/livehost_registry.unregister() cleanly,
    # without an UnboundLocalError on `current_turn` masking the original exception
    # and skipping cleanup.
    from starlette.websockets import WebSocket

    from app.services.livehost.registry import livehost_registry

    original_send_json = WebSocket.send_json

    async def flaky_send_json(self, data, mode="text"):
        if isinstance(data, dict) and data.get("event") == "session_started":
            raise RuntimeError("client disconnected before session_started could be sent")
        return await original_send_json(self, data, mode=mode)

    monkeypatch.setattr(WebSocket, "send_json", flaky_send_json)

    client = TestClient(app)
    session_id = "test-session-started-send-failure"
    url = (
        "/v1/livehost/stream?stt_engine=stub-livehost&tts_engine=stub-livehost-tts"
        f"&sample_rate=16000&session_id={session_id}"
    )
    try:
        with client.websocket_connect(url) as ws:
            # Block until the server-side task actually reaches the send_json
            # call and raises. Exiting the `with` block immediately would just
            # send a disconnect before the server got that far, never
            # exercising the fault we're injecting.
            ws.receive()
    except Exception:
        # The server-side handler raises (in the buggy version, an
        # UnboundLocalError masking the original RuntimeError); whether/how the
        # test transport surfaces that to the client isn't the point of this
        # test -- what matters is the registry cleanup below.
        pass

    assert livehost_registry.get(session_id) is None  # no leak from the masked-exception regression


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
