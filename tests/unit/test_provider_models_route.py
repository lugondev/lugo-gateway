import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.api.routes import providers as providers_route


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


def test_parse_models_openai_shape():
    ids = providers_route._parse_models(
        {"object": "list", "data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}, {"id": ""}, {"nope": 1}]}
    )
    assert ids == ["gpt-4o", "gpt-4o-mini"]


def test_models_route_returns_ids(client, _with_password, monkeypatch):
    _login_admin(client)
    prov = client.post("/v1/providers", json={
        "name": "openai", "base_url": "https://api.openai.com/v1", "api_key": "sk-x",
    }).json()["data"]

    async def fake_fetch(base_url, api_key):
        assert base_url == "https://api.openai.com/v1" and api_key == "sk-x"
        return (["gpt-4o", "gpt-4o-mini"], None)
    monkeypatch.setattr(providers_route, "_fetch_provider_models", fake_fetch)

    resp = client.get(f"/v1/providers/{prov['id']}/models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["models"] == ["gpt-4o", "gpt-4o-mini"]
    assert body["data"]["error"] is None


def test_models_route_fetch_error_is_200_empty(client, _with_password, monkeypatch):
    _login_admin(client)
    prov = client.post("/v1/providers", json={"name": "down", "base_url": "http://x/v1", "api_key": ""}).json()["data"]

    async def boom(base_url, api_key):
        return ([], "connect timeout")
    monkeypatch.setattr(providers_route, "_fetch_provider_models", boom)

    resp = client.get(f"/v1/providers/{prov['id']}/models")
    assert resp.status_code == 200
    assert resp.json()["data"] == {"models": [], "error": "connect timeout"}


def test_models_route_unknown_provider_404(client, _with_password):
    _login_admin(client)
    assert client.get("/v1/providers/does-not-exist/models").status_code == 404
