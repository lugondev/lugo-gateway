import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_guard_noop_when_admin_password_unset(client):
    assert settings.admin_password == ""
    resp = client.get("/v1/system/status")
    assert resp.status_code != 401


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def test_guard_blocks_system_route_when_logged_out(client, _with_password):
    resp = client.get("/v1/system/status")
    assert resp.status_code == 401


def test_guard_blocks_models_route_when_logged_out(client, _with_password):
    resp = client.get("/v1/models")
    assert resp.status_code == 401


def test_guard_allows_system_route_after_login(client, _with_password):
    client.post("/api/auth/signup", json={"username": "toan", "password": "s3cret"})
    client.post("/api/auth/login", json={"username": "toan", "password": "s3cret"})
    resp = client.get("/v1/system/status")
    assert resp.status_code != 401


def test_guard_allows_device_routes_without_login(client, _with_password):
    resp = client.get("/v1/stt/engines")
    assert resp.status_code != 401


def test_guard_allows_auth_routes_without_login(client, _with_password):
    resp = client.get("/api/auth/status")
    assert resp.status_code != 401


def test_guard_allows_options_preflight_without_login(client, _with_password):
    resp = client.options("/v1/system/status")
    assert resp.status_code != 401


class _FakeWebSocket:
    def __init__(self, session: dict | None = None, query_params: dict | None = None):
        self.session = session or {}
        self.query_params = query_params or {}


def test_ws_auth_noop_when_admin_password_unset():
    from app.core.auth_guard import ws_authenticated

    assert settings.admin_password == ""
    assert ws_authenticated(_FakeWebSocket()) is True


def test_ws_auth_accepts_valid_browser_cookie_session(_with_password):
    from app.core.auth_guard import ws_authenticated

    assert ws_authenticated(_FakeWebSocket(session={"authenticated": True})) is True


def test_ws_auth_rejects_missing_cookie_and_missing_token(_with_password):
    from app.core.auth_guard import ws_authenticated

    assert ws_authenticated(_FakeWebSocket()) is False


def test_ws_auth_accepts_valid_device_token(_with_password, monkeypatch):
    from app.core.auth_guard import ws_authenticated

    monkeypatch.setattr(settings, "device_auth_token", "d3vice-secret")
    ws = _FakeWebSocket(query_params={"device_token": "d3vice-secret"})
    assert ws_authenticated(ws) is True


def test_ws_auth_rejects_wrong_device_token(_with_password, monkeypatch):
    from app.core.auth_guard import ws_authenticated

    monkeypatch.setattr(settings, "device_auth_token", "d3vice-secret")
    ws = _FakeWebSocket(query_params={"device_token": "wrong"})
    assert ws_authenticated(ws) is False


def test_ws_auth_rejects_device_token_when_none_configured(_with_password):
    from app.core.auth_guard import ws_authenticated

    ws = _FakeWebSocket(query_params={"device_token": "anything"})
    assert ws_authenticated(ws) is False
