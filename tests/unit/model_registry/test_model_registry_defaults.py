from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.model_registry.store import model_registry_store


def _client():
    return TestClient(app)


def _login(client, username="u", role="user"):
    import asyncio
    from app.services.auth.users import user_store
    client.post("/api/auth/signup", json={"username": username, "password": "pw"})
    if role == "admin":
        u = asyncio.run(user_store.get_by_username(username))
        asyncio.run(user_store.set_fields(u.id, role="admin"))
    client.post("/api/auth/login", json={"username": username, "password": "pw"})


def test_defaults_shape_and_user_accessible(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    client = _client()
    _login(client, role="user")  # NON-admin: /defaults must be reachable
    resp = client.get("/v1/model_registry/defaults")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert set(data.keys()) == {"stt", "tts", "llm"}
    assert "engine" in data["stt"] and "label" in data["stt"]
    assert "engine" in data["tts"] and "label" in data["tts"]
    # llm is null unless an is_default llm entry exists
    assert data["llm"] is None


async def test_defaults_reflect_default_llm_entry():
    await model_registry_store.create("llm", "openrouter", "x/y", "My LLM", is_default=True)
    from app.api.routes.model_registry import get_defaults

    result = await get_defaults()
    assert result["success"] is True
    llm = result["data"]["llm"]
    assert llm == {"engine": "openrouter", "model_id": "x/y", "label": "My LLM"}
