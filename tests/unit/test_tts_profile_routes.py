import asyncio

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.model_registry.store import ModelRegistryStore
from app.services.tts.profile_store import TtsProfileStore


@pytest.fixture(autouse=True)
def _clean_store(tmp_path, monkeypatch):
    fresh = TtsProfileStore(str(tmp_path / "tts_profiles.json"))
    monkeypatch.setattr("app.api.routes.tts_profiles.tts_profile_store", fresh)


@pytest.fixture(autouse=True)
def _catalog_engines():
    # Catalog-mode (Task 4): TTS create/update gate the chosen engine against an
    # enabled registry entry (check_model_allowed("tts", engine, engine, ...)).
    # These route tests exercise CRUD, not the gate, so seed the engines they
    # use as catalogued/enabled. The route's singleton store reads these back
    # from the shared per-test tmp DB (see conftest `_tmp_db`). Tests that save
    # a profile with no engine (del/visible) skip the gate and don't need this.
    store = ModelRegistryStore()
    asyncio.run(store.create("tts", "vieneu", "vieneu", "VieNeu"))
    asyncio.run(store.create("tts", "omnivoice", "omnivoice", "OmniVoice"))


@pytest.fixture
def client():
    return TestClient(app)


def test_list_tts_profiles_empty(client):
    resp = client.get("/v1/tts/profiles")
    assert resp.status_code == 200
    assert resp.json()["data"] == {}


def test_create_tts_profile(client):
    payload = {"name": "test", "engine": "vieneu", "voice_mode": "preset", "voice": "v1"}
    resp = client.post("/v1/tts/profiles", json=payload)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["name"] == "test"
    assert data["engine"] == "vieneu"


def test_get_tts_profile(client):
    client.post("/v1/tts/profiles", json={"name": "x", "engine": "vieneu"})
    resp = client.get("/v1/tts/profiles/x")
    assert resp.status_code == 200
    assert resp.json()["data"]["engine"] == "vieneu"


def test_get_missing_tts_profile_404(client):
    resp = client.get("/v1/tts/profiles/ghost")
    assert resp.status_code == 404


def test_update_tts_profile(client):
    client.post("/v1/tts/profiles", json={"name": "upd", "engine": "vieneu"})
    resp = client.put("/v1/tts/profiles/upd", json={"name": "upd", "engine": "omnivoice"})
    assert resp.status_code == 200
    assert resp.json()["data"]["engine"] == "omnivoice"


def test_update_uses_path_name(client):
    resp = client.put("/v1/tts/profiles/canonical", json={"name": "ignored", "engine": "vieneu"})
    assert resp.json()["data"]["name"] == "canonical"


def test_delete_tts_profile(client):
    client.post("/v1/tts/profiles", json={"name": "del"})
    resp = client.delete("/v1/tts/profiles/del")
    assert resp.status_code == 200
    assert resp.json()["data"]["deleted"] is True
    assert client.get("/v1/tts/profiles/del").status_code == 404


def test_list_shows_created_tts_profile(client):
    client.post("/v1/tts/profiles", json={"name": "visible"})
    resp = client.get("/v1/tts/profiles")
    assert "visible" in resp.json()["data"]


def test_create_clone_tts_profile(client):
    payload = {
        "name": "cloned", "engine": "omnivoice", "voice_mode": "clone",
        "ref_audio_path": "artifacts/refs/host.wav", "ref_text": "hello there",
        "instruct": "cheerful", "speed": 1.2, "language": "vi",
    }
    resp = client.post("/v1/tts/profiles", json=payload)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["voice_mode"] == "clone"
    assert data["ref_audio_path"] == "artifacts/refs/host.wav"
    assert data["speed"] == 1.2
