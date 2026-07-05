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
    client.post("/api/auth/login", json={"password": "s3cret"})
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
