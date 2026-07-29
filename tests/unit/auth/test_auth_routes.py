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


def test_signup_then_login_sets_session(client, _with_password):
    resp = client.post("/api/auth/signup", json={"username": "toan", "password": "s3cret"})
    assert resp.status_code == 200

    resp = client.post("/api/auth/login", json={"username": "toan", "password": "s3cret"})
    assert resp.status_code == 200

    status = client.get("/api/auth/status").json()
    assert status["authenticated"] is True
    assert status["username"] == "toan"
    assert status["role"] == "user"
    assert status["can_use_testing"] is False


def test_signup_duplicate_username_rejected(client, _with_password):
    client.post("/api/auth/signup", json={"username": "toan", "password": "pw1"})
    resp = client.post("/api/auth/signup", json={"username": "toan", "password": "pw2"})
    assert resp.status_code == 409


def test_login_wrong_password_rejected(client, _with_password):
    client.post("/api/auth/signup", json={"username": "toan", "password": "s3cret"})
    resp = client.post("/api/auth/login", json={"username": "toan", "password": "nope"})
    assert resp.status_code == 401
    assert resp.json()["success"] is False


def test_login_unknown_username_rejected(client, _with_password):
    resp = client.post("/api/auth/login", json={"username": "ghost", "password": "x"})
    assert resp.status_code == 401


def test_logout_clears_session(client, _with_password):
    client.post("/api/auth/signup", json={"username": "toan", "password": "s3cret"})
    client.post("/api/auth/login", json={"username": "toan", "password": "s3cret"})
    client.post("/api/auth/logout")
    assert client.get("/api/auth/status").json() == {"authenticated": False}


def test_disabled_user_cannot_login(client, _with_password):
    from app.services.auth.users import user_store

    created = client.post(
        "/api/auth/signup", json={"username": "toan", "password": "s3cret"}
    )
    assert created.status_code == 200

    import asyncio

    asyncio.run(user_store.set_fields(_user_id_for("toan"), disabled=True))
    resp = client.post("/api/auth/login", json={"username": "toan", "password": "s3cret"})
    assert resp.status_code == 401


def _user_id_for(username: str) -> str:
    import asyncio

    from app.services.auth.users import user_store

    user = asyncio.run(user_store.get_by_username(username))
    return user.id
