import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.stt.service import stt_service
from app.services.tts.service import tts_service


@pytest.fixture
def client():
    return TestClient(app)


class _ColdProvider:
    """A provider whose warm() hasn't run yet — simulates a cold model load."""

    name = "fake"

    def warm(self) -> None:
        pass

    def detail(self) -> str:
        return "fake"


def test_session_started_reports_cold_engines_then_sends_engines_ready(client, monkeypatch):
    fake_stt, fake_tts = _ColdProvider(), _ColdProvider()
    monkeypatch.setitem(stt_service.providers, "whisper", fake_stt)
    monkeypatch.setitem(stt_service.providers, "whisper_local", fake_stt)
    monkeypatch.setitem(tts_service.providers, "vieneu", fake_tts)

    with client.websocket_connect("/v1/conversation/stream?output=text") as ws:
        started = ws.receive_json()
        assert started["event"] == "session_started"
        assert started["stt_ready"] is False
        assert started["tts_ready"] is False

        notify = ws.receive_json()
        assert notify == {"event": "engines_ready"}


def test_session_started_reports_ready_when_already_warm(client, monkeypatch):
    from app.services.warmup import _ready_ids

    fake_stt, fake_tts = _ColdProvider(), _ColdProvider()
    _ready_ids.add(id(fake_stt))
    _ready_ids.add(id(fake_tts))
    monkeypatch.setitem(stt_service.providers, "whisper", fake_stt)
    monkeypatch.setitem(stt_service.providers, "whisper_local", fake_stt)
    monkeypatch.setitem(tts_service.providers, "vieneu", fake_tts)

    with client.websocket_connect("/v1/conversation/stream?output=text") as ws:
        started = ws.receive_json()
        assert started["stt_ready"] is True
        assert started["tts_ready"] is True
