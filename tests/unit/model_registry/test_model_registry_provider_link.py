import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.conversation.responder import OpenAICompatResponder


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


def test_llm_entry_test_call_uses_provider_creds(client, _with_password, monkeypatch):
    _login_admin(client)
    # a provider with the real endpoint + key
    prov = client.post("/v1/providers", json={
        "name": "openrouter", "base_url": "https://openrouter.ai/api/v1", "api_key": "sk-or-REAL",
    }).json()["data"]

    captured = {}

    async def fake_reply(self, history):
        captured["base_url"] = self.base_url
        captured["api_key"] = self.api_key
        return "ok"

    async def fake_close(self):
        return None

    monkeypatch.setattr(OpenAICompatResponder, "reply", fake_reply)
    monkeypatch.setattr(OpenAICompatResponder, "aclose", fake_close)

    # create an llm model that references the provider, leaving base_url/api_key blank
    resp = client.post("/v1/model_registry", json={
        "kind": "llm", "engine": "openrouter", "model_id": "qwen/qwen-2.5-72b-instruct",
        "label": "Qwen 2.5 72B", "config": {"provider_id": prov["id"]},
    })
    assert resp.status_code == 200, resp.text
    assert captured["base_url"] == "https://openrouter.ai/api/v1"
    assert captured["api_key"] == "sk-or-REAL"
