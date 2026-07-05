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


def test_status_unauthenticated_by_default(client, _with_password):
    resp = client.get("/api/auth/status")
    assert resp.status_code == 200
    assert resp.json() == {"authenticated": False}


def test_login_wrong_password_rejected(client, _with_password):
    resp = client.post("/api/auth/login", json={"password": "nope"})
    assert resp.status_code == 401
    assert resp.json()["success"] is False


def test_login_correct_password_sets_session(client, _with_password):
    resp = client.post("/api/auth/login", json={"password": "s3cret"})
    assert resp.status_code == 200
    assert client.get("/api/auth/status").json() == {"authenticated": True}


def test_logout_clears_session(client, _with_password):
    client.post("/api/auth/login", json={"password": "s3cret"})
    client.post("/api/auth/logout")
    assert client.get("/api/auth/status").json() == {"authenticated": False}
