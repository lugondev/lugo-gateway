import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.profiles.store import ProfileStore, profile_store


@pytest.fixture(autouse=True)
def _clean_store(tmp_path, monkeypatch):
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    monkeypatch.setattr("app.api.routes.profiles.profile_store", fresh)
    monkeypatch.setattr("app.services.profiles.store.profile_store", fresh)


@pytest.fixture
def client():
    return TestClient(app)


def test_list_profiles_empty(client):
    resp = client.get("/v1/profiles")
    assert resp.status_code == 200
    assert resp.json()["data"] == {}


def test_create_profile(client):
    payload = {
        "name": "test",
        "system_prompt": "Be brief.",
        "llm": {"base_url": "http://localhost:11434/v1", "api_key": "", "model": "llama3.2"},
        "tts": {"engine": "vieneu", "voice": ""},
        "mcp_servers": [],
    }
    resp = client.post("/v1/profiles", json=payload)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["name"] == "test"
    assert data["system_prompt"] == "Be brief."


def test_get_profile(client):
    client.post("/v1/profiles", json={"name": "x", "system_prompt": "hello"})
    resp = client.get("/v1/profiles/x")
    assert resp.status_code == 200
    assert resp.json()["data"]["system_prompt"] == "hello"


def test_get_missing_profile_404(client):
    resp = client.get("/v1/profiles/ghost")
    assert resp.status_code == 404


def test_update_profile(client):
    client.post("/v1/profiles", json={"name": "upd", "system_prompt": "old"})
    resp = client.put("/v1/profiles/upd", json={"name": "upd", "system_prompt": "new"})
    assert resp.status_code == 200
    assert resp.json()["data"]["system_prompt"] == "new"


def test_update_uses_path_name(client):
    # path param wins over body name
    resp = client.put("/v1/profiles/canonical", json={"name": "ignored", "system_prompt": "x"})
    assert resp.json()["data"]["name"] == "canonical"


def test_delete_profile(client):
    client.post("/v1/profiles", json={"name": "del"})
    resp = client.delete("/v1/profiles/del")
    assert resp.status_code == 200
    assert resp.json()["data"]["deleted"] is True
    assert client.get("/v1/profiles/del").status_code == 404


def test_list_shows_created_profile(client):
    client.post("/v1/profiles", json={"name": "visible"})
    resp = client.get("/v1/profiles")
    assert "visible" in resp.json()["data"]
