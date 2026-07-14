import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.auth.users import user_store
from app.services.model_registry.store import ModelRegistryStore
from app.services.profiles.store import ProfileStore, profile_store


@pytest.fixture(autouse=True)
def _clean_store(tmp_path, monkeypatch):
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    monkeypatch.setattr("app.api.routes.profiles.profile_store", fresh)
    monkeypatch.setattr("app.services.profiles.store.profile_store", fresh)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _signup_login(client, username: str, role: str = "user") -> None:
    client.post("/api/auth/signup", json={"username": username, "password": "pw"})
    if role == "admin":
        user = asyncio.run(user_store.get_by_username(username))
        asyncio.run(user_store.set_fields(user.id, role="admin"))
    client.post("/api/auth/login", json={"username": username, "password": "pw"})


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


def test_llm_options_lists_enabled_stable_entries_for_regular_user(client, _with_password):
    store = ModelRegistryStore()
    asyncio.run(store.create("llm", "openai", "gpt-4o-mini", "GPT-4o mini", stage="stable"))
    _signup_login(client, "toan", role="user")
    resp = client.get("/v1/profiles/llm-options")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert any(e["engine"] == "openai" and e["model_id"] == "gpt-4o-mini" for e in data)
    entry = next(e for e in data if e["engine"] == "openai")
    assert "api_key" not in entry
    assert "base_url" not in entry


def test_llm_options_hides_testing_stage_for_non_tester(client, _with_password):
    store = ModelRegistryStore()
    asyncio.run(store.create("llm", "openrouter", "qwen3-instruct", "Qwen3 Instruct", stage="testing"))
    _signup_login(client, "toan2", role="user")
    resp = client.get("/v1/profiles/llm-options")
    assert resp.status_code == 200
    assert not any(e["engine"] == "openrouter" for e in resp.json()["data"])


def test_llm_options_shows_testing_stage_for_tester(client, _with_password):
    store = ModelRegistryStore()
    asyncio.run(store.create("llm", "openrouter", "qwen3-instruct-2", "Qwen3 Instruct 2", stage="testing"))
    _signup_login(client, "toan3", role="user")
    user = asyncio.run(user_store.get_by_username("toan3"))
    asyncio.run(user_store.set_fields(user.id, can_use_testing=True))
    resp = client.get("/v1/profiles/llm-options")
    assert resp.status_code == 200
    assert any(e["engine"] == "openrouter" and e["model_id"] == "qwen3-instruct-2" for e in resp.json()["data"])


def test_llm_options_hides_disabled_entries(client, _with_password):
    store = ModelRegistryStore()
    entry = asyncio.run(store.create("llm", "openai", "gpt-disabled", "Disabled model", stage="stable"))
    asyncio.run(store.set_fields(entry["id"], enabled=False))
    _signup_login(client, "toan4", role="user")
    resp = client.get("/v1/profiles/llm-options")
    assert resp.status_code == 200
    assert not any(e["engine"] == "openai" and e["model_id"] == "gpt-disabled" for e in resp.json()["data"])
