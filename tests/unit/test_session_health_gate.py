import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.health import EngineHealth


@pytest.fixture
def client():
    return TestClient(app)


def _patch_health(monkeypatch, stt_status: str, tts_status: str, detail: str = "boom"):
    async def fake(stt_engine, stt_model, tts_engine, tts_model):
        return (
            EngineHealth(engine=stt_engine, status=stt_status, detail=detail),
            EngineHealth(engine=tts_engine, status=tts_status, detail=detail),
        )

    monkeypatch.setattr("app.api.routes.conversation.check_resolved_engines", fake)
    monkeypatch.setattr("app.api.routes.lugo.check_resolved_engines", fake)


def test_conversation_ws_rejected_when_stt_unavailable(client, monkeypatch):
    _patch_health(monkeypatch, "unavailable", "ok", detail="unreachable at http://x")
    with client.websocket_connect("/v1/conversation/stream") as ws:
        msg = ws.receive_json()
        assert msg["event"] == "error"
        assert "unreachable at http://x" in msg["message"]


def test_conversation_ws_rejected_when_tts_unavailable(client, monkeypatch):
    _patch_health(monkeypatch, "ok", "unavailable", detail="no base_url")
    with client.websocket_connect("/v1/conversation/stream") as ws:
        msg = ws.receive_json()
        assert msg["event"] == "error"
        assert "no base_url" in msg["message"]


def test_conversation_ws_allowed_when_engines_not_ready(client, monkeypatch):
    """not_ready = still warming, not broken -- must NOT block the session."""
    _patch_health(monkeypatch, "not_ready", "not_ready")
    with client.websocket_connect("/v1/conversation/stream") as ws:
        msg = ws.receive_json()
        assert msg["event"] != "error"


def test_conversation_ws_allowed_when_all_ok(client, monkeypatch):
    _patch_health(monkeypatch, "ok", "ok")
    with client.websocket_connect("/v1/conversation/stream") as ws:
        msg = ws.receive_json()
        assert msg["event"] != "error"


def test_lugo_ws_rejected_when_stt_unavailable(client, monkeypatch):
    _patch_health(monkeypatch, "unavailable", "ok", detail="unreachable")
    with client.websocket_connect("/v1/lugo/stream") as ws:
        ws.send_text(json.dumps({"type": "wakeup", "profile": None}))
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "unreachable" in msg["message"]


def test_lugo_ws_allowed_when_all_ok(client, monkeypatch):
    _patch_health(monkeypatch, "ok", "ok")
    with client.websocket_connect("/v1/lugo/stream") as ws:
        ws.send_text(json.dumps({"type": "wakeup", "profile": None}))
        msg = ws.receive_json()
        assert msg["type"] != "error"
