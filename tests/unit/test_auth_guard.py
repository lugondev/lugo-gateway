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


def _login_as(client, username: str, password: str, role: str = "user") -> None:
    client.post("/api/auth/signup", json={"username": username, "password": password})
    if role == "admin":
        import asyncio

        from app.services.auth.users import user_store

        user = asyncio.run(user_store.get_by_username(username))
        asyncio.run(user_store.set_fields(user.id, role="admin"))
    client.post("/api/auth/login", json={"username": username, "password": password})


def test_guard_blocks_admin_route_when_logged_out(client, _with_password):
    resp = client.get("/v1/system/status")
    assert resp.status_code == 401


def test_guard_403s_admin_route_for_regular_user(client, _with_password):
    _login_as(client, "toan", "s3cret", role="user")
    resp = client.get("/v1/system/status")
    assert resp.status_code == 403


def test_guard_allows_admin_route_for_admin(client, _with_password):
    _login_as(client, "root", "s3cret", role="admin")
    resp = client.get("/v1/system/status")
    assert resp.status_code != 401
    assert resp.status_code != 403


def test_guard_allows_user_route_for_regular_user(client, _with_password):
    _login_as(client, "toan", "s3cret", role="user")
    resp = client.get("/v1/profiles")
    assert resp.status_code != 401
    assert resp.status_code != 403


def test_guard_blocks_user_route_when_logged_out(client, _with_password):
    resp = client.get("/v1/profiles")
    assert resp.status_code == 401


def test_guard_allows_device_pairing_init_without_login(client, _with_password):
    resp = client.post("/v1/devices/pair/init", json={"serial": "AA:BB:CC"})
    assert resp.status_code != 401


def test_guard_blocks_pair_claim_when_logged_out(client, _with_password):
    resp = client.post("/v1/devices/pair/claim", json={"code": "000000", "name": "x"})
    assert resp.status_code == 401


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
