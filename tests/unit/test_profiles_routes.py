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
        "tts": {"profile_name": "cohost-voice"},
        "mcp_servers": [],
    }
    resp = client.post("/v1/profiles", json=payload)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["name"] == "test"
    assert data["system_prompt"] == "Be brief."


def test_voice_optimized_defaults_false_and_round_trips(client):
    client.post("/v1/profiles", json={"name": "plain"})
    assert client.get("/v1/profiles/plain").json()["data"]["voice_optimized"] is False

    client.post("/v1/profiles", json={"name": "voice", "voice_optimized": True})
    assert client.get("/v1/profiles/voice").json()["data"]["voice_optimized"] is True


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


def test_update_preserves_api_key_when_blank(client):
    # Create with a real key, then update with a BLANK key (as the UI does on edit,
    # where the password field is empty = "keep existing"). The key must survive.
    client.post("/v1/profiles", json={
        "name": "keyed",
        "llm": {"base_url": "https://api.openai.com/v1", "api_key": "sk-secret-123", "model": "gpt-x"},
    })
    resp = client.put("/v1/profiles/keyed", json={
        "name": "keyed",
        "system_prompt": "changed",
        "llm": {"base_url": "https://api.openai.com/v1", "api_key": "", "model": "gpt-x"},
    })
    assert resp.status_code == 200
    from app.services.profiles.store import profile_store as ps
    assert ps.get("keyed").llm.api_key == "sk-secret-123"  # preserved, not wiped


def test_update_replaces_api_key_when_provided(client):
    client.post("/v1/profiles", json={
        "name": "keyed2",
        "llm": {"base_url": "u", "api_key": "old-key", "model": "m"},
    })
    client.put("/v1/profiles/keyed2", json={
        "name": "keyed2",
        "llm": {"base_url": "u", "api_key": "new-key", "model": "m"},
    })
    from app.services.profiles.store import profile_store as ps
    assert ps.get("keyed2").llm.api_key == "new-key"


def test_create_profile_accepts_valid_stt_model(client):
    resp = client.post("/v1/profiles", json={
        "name": "good-model",
        "stt": {"engine": "qwen3_asr", "model": "0.6b"},
    })
    assert resp.status_code == 200
    assert resp.json()["data"]["stt"]["model"] == "0.6b"


def test_create_profile_rejects_invalid_stt_model(client):
    # WhisperManager.validate() checks both filename-safety (`^[A-Za-z0-9.\-]+$`)
    # and membership in the closed set of known whisper sizes, so an
    # unlisted-but-well-formed size is enough to trigger rejection.
    resp = client.post("/v1/profiles", json={
        "name": "bad-model",
        "stt": {"engine": "whisper", "model": "not-a-real-whisper-size"},
    })
    assert resp.status_code == 400


def test_create_profile_rejects_model_on_engine_without_registry(client):
    resp = client.post("/v1/profiles", json={
        "name": "no-variants",
        "stt": {"engine": "vosk", "model": "anything"},
    })
    assert resp.status_code == 400


def test_create_profile_rejects_model_without_resolvable_engine(client):
    resp = client.post("/v1/profiles", json={
        "name": "no-engine",
        "stt": {"model": "0.6b"},
    })
    assert resp.status_code == 400


def test_create_profile_model_resolves_engine_via_preset(client):
    # "vi" preset resolves to qwen3_asr — model should validate against that.
    resp = client.post("/v1/profiles", json={
        "name": "preset-model",
        "stt": {"profile": "vi", "model": "1.7b"},
    })
    assert resp.status_code == 200


def test_update_profile_rejects_invalid_stt_model(client):
    client.post("/v1/profiles", json={"name": "upd-model", "stt": {"engine": "qwen3_asr"}})
    resp = client.put("/v1/profiles/upd-model", json={
        "name": "upd-model",
        "stt": {"engine": "qwen3_asr", "model": "nope"},
    })
    assert resp.status_code == 400
