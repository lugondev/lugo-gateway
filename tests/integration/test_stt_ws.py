import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.stt import STTResult
from app.services.stt.base import STTProvider, STTStream
from app.services.stt.service import stt_service


class _EchoStream(STTStream):
    def __init__(self) -> None:
        self._frames = 0

    async def accept(self, pcm: bytes) -> list[STTResult]:
        self._frames += 1
        return [STTResult(engine="echo", text=f"partial {self._frames}", is_final=False)]

    async def finalize(self) -> STTResult | None:
        return STTResult(engine="echo", text="final transcript", is_final=True)


class _EchoProvider(STTProvider):
    name = "echo"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="batch", is_final=True)

    def open_stream(self, sample_rate, language=None, model=None) -> STTStream:
        return _EchoStream()


@pytest.fixture(autouse=True)
def _register_echo():
    stt_service.providers["echo"] = _EchoProvider()
    yield
    stt_service.providers.pop("echo", None)


def test_ws_stream_partial_then_final_then_done():
    client = TestClient(app)
    with client.websocket_connect("/v1/stt/stream?engine=echo&sample_rate=16000") as ws:
        started = ws.receive_json()
        assert started["event_type"] == "session_started"

        ws.send_bytes(b"\x00\x00" * 100)
        partial = ws.receive_json()
        assert partial["event_type"] == "partial"
        assert partial["payload"]["text"] == "partial 1"

        ws.send_text('{"type": "end"}')
        final = ws.receive_json()
        assert final["event_type"] == "final"
        assert final["payload"]["text"] == "final transcript"

        done = ws.receive_json()
        assert done["event_type"] == "done"


def test_ws_stream_unknown_engine_emits_error():
    client = TestClient(app)
    with client.websocket_connect("/v1/stt/stream?engine=nope") as ws:
        event = ws.receive_json()
        assert event["event_type"] == "error"
