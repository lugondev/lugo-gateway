import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.auth.tokens import verify_access_token, verify_refresh_token
from app.services.auth.users import user_store


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
async def user():
    return await user_store.create("token-route-user", "pw12345678", role="user")


async def test_token_endpoint_returns_both_tokens(client, user):
    resp = client.post(
        "/api/auth/token",
        json={"username": "token-route-user", "password": "pw12345678"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert verify_access_token(data["access_token"]) == user["id"]
    assert verify_refresh_token(data["refresh_token"]) == user["id"]
    assert data["expires_in"] == 3600


async def test_token_endpoint_rejects_bad_password(client, user):
    resp = client.post(
        "/api/auth/token",
        json={"username": "token-route-user", "password": "wrong-password"},
    )
    assert resp.status_code != 200


async def test_token_endpoint_rejects_disabled_user(client, user):
    await user_store.set_fields(user["id"], disabled=True)
    resp = client.post(
        "/api/auth/token",
        json={"username": "token-route-user", "password": "pw12345678"},
    )
    assert resp.status_code != 200


async def test_token_endpoint_does_not_set_session_cookie(client, user):
    """Đường bearer phải tách hẳn khỏi cookie. Nếu endpoint này set cookie thì
    web client vô tình có hai danh tính song song."""
    resp = client.post(
        "/api/auth/token",
        json={"username": "token-route-user", "password": "pw12345678"},
    )
    assert "set-cookie" not in {k.lower() for k in resp.headers}


async def test_refresh_returns_new_access_token(client, user):
    tokens = client.post(
        "/api/auth/token",
        json={"username": "token-route-user", "password": "pw12345678"},
    ).json()["data"]
    resp = client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert resp.status_code == 200
    assert verify_access_token(resp.json()["data"]["access_token"]) == user["id"]


async def test_refresh_rejects_access_token_as_refresh(client, user):
    tokens = client.post(
        "/api/auth/token",
        json={"username": "token-route-user", "password": "pw12345678"},
    ).json()["data"]
    resp = client.post("/api/auth/refresh", json={"refresh_token": tokens["access_token"]})
    assert resp.status_code != 200


async def test_refresh_rejects_garbage(client):
    resp = client.post("/api/auth/refresh", json={"refresh_token": "garbage"})
    assert resp.status_code != 200


async def test_refresh_rejects_disabled_user(client, user):
    """Thu hồi không tức thì với access token (TTL 1h), nhưng refresh PHẢI
    kiểm tra lại -- nếu không, user bị vô hiệu hoá vẫn gia hạn được vĩnh viễn."""
    tokens = client.post(
        "/api/auth/token",
        json={"username": "token-route-user", "password": "pw12345678"},
    ).json()["data"]
    await user_store.set_fields(user["id"], disabled=True)
    resp = client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert resp.status_code != 200
