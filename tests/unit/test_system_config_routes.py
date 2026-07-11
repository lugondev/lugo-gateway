import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.system_config import SystemConfigStore


@pytest.fixture(autouse=True)
def _clean_store(tmp_path, monkeypatch):
    fresh = SystemConfigStore(str(tmp_path / "system_config.json"))
    monkeypatch.setattr("app.api.routes.system.system_config_store", fresh)
    return fresh


@pytest.fixture
def client():
    return TestClient(app)


def test_get_config_defaults_empty(client):
    resp = client.get("/v1/system/config")
    assert resp.status_code == 200
    assert resp.json()["data"]["base_context"] == ""


def test_set_config_base_context(client):
    resp = client.put("/v1/system/config", json={"base_context": "Platform: TeguVoice."})
    assert resp.status_code == 200
    assert resp.json()["data"]["base_context"] == "Platform: TeguVoice."
    assert client.get("/v1/system/config").json()["data"]["base_context"] == "Platform: TeguVoice."


def test_set_config_clears_base_context(client):
    client.put("/v1/system/config", json={"base_context": "something"})
    resp = client.put("/v1/system/config", json={"base_context": ""})
    assert resp.json()["data"]["base_context"] == ""


def test_get_config_defaults_openrouter_api_key_empty(client):
    resp = client.get("/v1/system/config")
    assert resp.json()["data"]["openrouter_api_key"] == ""


def test_set_openrouter_api_key_is_masked_in_response(client):
    resp = client.put("/v1/system/config", json={"openrouter_api_key": "sk-or-real-secret"})
    assert resp.json()["data"]["openrouter_api_key"] == "***"
    assert client.get("/v1/system/config").json()["data"]["openrouter_api_key"] == "***"


def test_blank_openrouter_api_key_preserves_existing(client):
    client.put("/v1/system/config", json={"openrouter_api_key": "sk-or-real-secret"})
    resp = client.put("/v1/system/config", json={"base_context": "x", "openrouter_api_key": ""})
    # Still masked (not empty) => the previously stored key was preserved, not wiped.
    assert resp.json()["data"]["openrouter_api_key"] == "***"
    assert resp.json()["data"]["base_context"] == "x"


def test_set_base_context_does_not_clear_openrouter_api_key(client):
    client.put("/v1/system/config", json={"openrouter_api_key": "sk-or-real-secret"})
    client.put("/v1/system/config", json={"base_context": "hello"})
    assert client.get("/v1/system/config").json()["data"]["openrouter_api_key"] == "***"
