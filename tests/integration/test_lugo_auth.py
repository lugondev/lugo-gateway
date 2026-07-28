import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app


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
    with client.websocket_connect("/v1/lugo/stream?device_token=d3vice-secret") as ws:
        ws.send_json({"type": "wakeup"})
        welcome = ws.receive_json()
        assert welcome["type"] == "welcome"
