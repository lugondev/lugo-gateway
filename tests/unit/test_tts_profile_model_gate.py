import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.model_registry.store import ModelRegistryStore
from app.services.tts.profile_store import TtsProfileStore


@pytest.fixture(autouse=True)
def _clean_store(tmp_path, monkeypatch):
    # Mirrors tests/unit/test_tts_profile_ownership.py / test_tts_profile_routes.py:
    # tts_profile_store is a module-level singleton with an in-memory cache that,
    # once populated, ignores the fresh per-test SQLite file the autouse
    # tests/conftest.py `_tmp_db` fixture points the engine at -- writes would
    # silently target a tableless DB, and profile names (e.g. "p1") would bleed
    # across tests in this file. A brand new TtsProfileStore (cache=None) per
    # test avoids that staleness.
    fresh = TtsProfileStore(str(tmp_path / "tts_profiles.json"))
    monkeypatch.setattr("app.api.routes.tts_profiles.tts_profile_store", fresh)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _signup_login(client, username: str) -> None:
    client.post("/api/auth/signup", json={"username": username, "password": "pw"})
    client.post("/api/auth/login", json={"username": username, "password": "pw"})


def test_tts_profile_create_rejects_disabled_engine(client, _with_password):
    store = ModelRegistryStore()
    entry = asyncio.run(store.create("tts", "omnivoice", "omnivoice", "OmniVoice"))
    asyncio.run(store.set_fields(entry["id"], enabled=False))

    _signup_login(client, "toan")
    resp = client.post("/v1/tts/profiles", json={"name": "p1", "engine": "omnivoice"})
    assert resp.status_code == 403


def test_tts_profile_create_rejects_engine_not_in_registry(client, _with_password):
    # Catalog-mode (Task 4): an engine with no enabled registry entry is now
    # rejected (was: silently allowed). Inverts the former
    # test_tts_profile_create_allows_engine_not_in_registry.
    _signup_login(client, "toan")
    resp = client.post("/v1/tts/profiles", json={"name": "p1", "engine": "some-future-engine"})
    assert resp.status_code == 403


def test_tts_profile_create_allows_catalogued_engine(client, _with_password):
    # The accept side of catalog-mode: an enabled entry for the engine lets the
    # save through. TTS gates on (engine, engine) -- see routes/tts_profiles.py.
    store = ModelRegistryStore()
    asyncio.run(store.create("tts", "vieneu", "vieneu", "VieNeu"))

    _signup_login(client, "toan")
    resp = client.post("/v1/tts/profiles", json={"name": "p1", "engine": "vieneu"})
    assert resp.status_code == 200
