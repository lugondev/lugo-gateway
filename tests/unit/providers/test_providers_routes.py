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


def _login_admin(client, username="adm"):
    import asyncio
    from app.services.auth.users import user_store
    client.post("/api/auth/signup", json={"username": username, "password": "pw"})
    user = asyncio.run(user_store.get_by_username(username))
    asyncio.run(user_store.set_fields(user.id, role="admin"))
    client.post("/api/auth/login", json={"username": username, "password": "pw"})


def test_regular_user_cannot_reach_providers(client, _with_password):
    client.post("/api/auth/signup", json={"username": "bob", "password": "pw"})
    client.post("/api/auth/login", json={"username": "bob", "password": "pw"})
    assert client.get("/v1/providers").status_code == 403


def test_admin_crud_and_key_masking(client, _with_password):
    _login_admin(client)
    # create
    resp = client.post("/v1/providers", json={
        "name": "openrouter", "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1", "api_key": "sk-or-v1-abcdefghijklmno",
    })
    assert resp.status_code == 200, resp.text
    created = resp.json()["data"]
    assert created["api_key"] != "sk-or-v1-abcdefghijklmno"   # masked
    assert "..." in created["api_key"]

    # list also masks
    listed = client.get("/v1/providers").json()["data"]
    assert any(p["id"] == created["id"] for p in listed)
    assert all("sk-or-v1-abcdefghijklmno" != p["api_key"] for p in listed)

    # patch with blank api_key keeps existing (no unmasking needed to test here)
    r = client.patch(f"/v1/providers/{created['id']}", json={"enabled": False, "api_key": ""})
    assert r.json()["data"]["enabled"] is False

    # delete
    assert client.delete(f"/v1/providers/{created['id']}").json()["data"]["deleted"] is True


def test_presets_endpoint(client, _with_password):
    _login_admin(client)
    data = client.get("/v1/providers/presets").json()["data"]
    assert {p["name"] for p in data} >= {"openai", "openrouter", "qwencloud"}
