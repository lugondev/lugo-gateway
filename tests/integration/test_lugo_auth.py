import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.schemas.health import EngineHealth


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def test_lugo_stream_rejects_unauthenticated_connect(client, _with_password):
    with pytest.raises(Exception):  # noqa: B017 -- TestClient raises on the 4401 close
        with client.websocket_connect("/v1/lugo/stream"):
            pass


def test_lugo_stream_accepts_valid_device_token(client, _with_password, monkeypatch):
    monkeypatch.setattr(settings, "device_auth_token", "d3vice-secret")

    async def _ok_health(stt_engine, stt_model, tts_engine, tts_model):
        return (
            EngineHealth(engine=stt_engine, status="ok"),
            EngineHealth(engine=tts_engine, status="ok"),
        )

    # This test connects with the system default STT/TTS engines
    # (vosk/omnivoice), which aren't actually configured in this hermetic
    # environment. Stub the Task 7 health gate so the connection isn't
    # refused before the auth behavior under test ever runs.
    monkeypatch.setattr("app.api.routes.lugo.check_resolved_engines", _ok_health)
    with client.websocket_connect("/v1/lugo/stream?device_token=d3vice-secret") as ws:
        ws.send_json({"type": "wakeup"})
        welcome = ws.receive_json()
        assert welcome["type"] == "welcome"
