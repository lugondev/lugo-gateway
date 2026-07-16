import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.auth.tokens import issue_access_token
from app.services.auth.users import user_store


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


@pytest.fixture
async def admin_user():
    created = await user_store.create("bearer-admin", "pw12345678", role="admin")
    return created


@pytest.fixture
async def normal_user():
    created = await user_store.create("bearer-user", "pw12345678", role="user")
    return created


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_bearer_grants_user_prefix(client, _with_password, normal_user):
    token = issue_access_token(normal_user["id"])
    resp = client.get("/v1/sessions", headers=_auth(token))
    assert resp.status_code != 401


async def test_no_bearer_is_rejected(client, _with_password):
    resp = client.get("/v1/sessions")
    assert resp.status_code == 401


async def test_invalid_bearer_is_rejected(client, _with_password):
    resp = client.get("/v1/sessions", headers=_auth("garbage"))
    assert resp.status_code == 401


async def test_bearer_for_unknown_user_is_rejected(client, _with_password):
    token = issue_access_token("no-such-user-id")
    resp = client.get("/v1/sessions", headers=_auth(token))
    assert resp.status_code == 401


async def test_bearer_for_disabled_user_is_rejected(client, _with_password, normal_user):
    await user_store.set_fields(normal_user["id"], disabled=True)
    token = issue_access_token(normal_user["id"])
    resp = client.get("/v1/sessions", headers=_auth(token))
    assert resp.status_code == 401


async def test_bearer_never_reaches_admin_prefix_even_for_admin_user(
    client, _with_password, admin_user
):
    """Ràng buộc cốt lõi: token của một user role=admin trong DB vẫn KHÔNG
    mở được đường admin, vì đường bearer hardcode role="user"."""
    token = issue_access_token(admin_user["id"])
    resp = client.get("/v1/system/status", headers=_auth(token))
    assert resp.status_code == 403


async def test_admin_prefix_still_works_via_session_cookie(client, _with_password, admin_user):
    """Admin webui không được hỏng."""
    client.post("/api/auth/login", json={"username": "bearer-admin", "password": "pw12345678"})
    resp = client.get("/v1/system/status")
    assert resp.status_code == 200
