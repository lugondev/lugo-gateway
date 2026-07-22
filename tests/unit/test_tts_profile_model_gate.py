import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.model_registry.seed import seed_installed_models_to_registry
from app.services.model_registry.store import ModelRegistryStore
from app.services.tts.profile_models import TtsProfile
from app.services.tts.profile_store import TtsProfileStore


@pytest.fixture(autouse=True)
def _clean_store(tmp_path, monkeypatch):
    # Mirrors tests/unit/test_tts_profile_ownership.py / test_tts_profile_routes.py:
    # tts_profile_store is a module-level singleton with an in-memory cache that,
    # once populated, ignores the fresh per-test SQLite file the autouse
    # tests/conftest.py `_tmp_db` fixture points the engine at -- writes would
    # silently target a tableless DB, and profile names (e.g. "p1") would bleed
    # across tests in this file. A brand new TtsProfileStore (cache=None) per
    # test avoids that staleness. Patched under both names so
    # seed_installed_models_to_registry()'s own `from
    # app.services.tts.profile_store import tts_profile_store` (Finding 1's fix)
    # sees the same fresh instance the route module writes through.
    fresh = TtsProfileStore(str(tmp_path / "tts_profiles.json"))
    monkeypatch.setattr("app.api.routes.tts_profiles.tts_profile_store", fresh)
    monkeypatch.setattr("app.services.tts.profile_store.tts_profile_store", fresh)


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


def test_tts_profile_create_rejects_disabled_row(client, _with_password):
    # Row-based gating: the profile selects a specific (engine, model_id) row,
    # so a disabled row is rejected -- mirrors routes/tts_profiles.py passing
    # profile.model_id (not the engine name) to check_model_allowed.
    store = ModelRegistryStore()
    entry = asyncio.run(store.create("tts", "omnivoice", "omnivoice", "OmniVoice"))
    asyncio.run(store.set_fields(entry["id"], enabled=False))

    _signup_login(client, "toan")
    resp = client.post(
        "/v1/tts/profiles",
        json={"name": "p1", "engine": "omnivoice", "model_id": "omnivoice"},
    )
    assert resp.status_code == 403


def test_tts_profile_create_rejects_row_not_in_registry(client, _with_password):
    # Catalog-mode: a concrete (engine, model_id) with no enabled registry entry
    # is rejected. This is exactly the http_tts/vieneu-cloudflare bug's inverse
    # -- the gate must check the selected row, not the engine name against itself.
    _signup_login(client, "toan")
    resp = client.post(
        "/v1/tts/profiles",
        json={"name": "p1", "engine": "http_tts", "model_id": "ghost-model"},
    )
    assert resp.status_code == 403


def test_tts_profile_create_allows_catalogued_row(client, _with_password):
    # The accept side of catalog-mode: an enabled row for the exact
    # (engine, model_id) lets the save through. Reproduces the fix for the
    # http_tts/vieneu-cloudflare profile-save failure.
    store = ModelRegistryStore()
    asyncio.run(store.create("tts", "http_tts", "vieneu-cloudflare", "VieNeu (Cloudflare)"))

    _signup_login(client, "toan")
    resp = client.post(
        "/v1/tts/profiles",
        json={"name": "p1", "engine": "http_tts", "model_id": "vieneu-cloudflare"},
    )
    assert resp.status_code == 200


def test_tts_profile_engine_only_is_not_gated(client, _with_password):
    # An engine with no model_id is the "inherit / first-enabled fallback" case
    # (the provider resolves the row via find_enabled). Like STT/LLM, the gate
    # short-circuits on empty model_id, so a legacy engine-only profile saves
    # without needing a (engine, engine) shim -- no boot-seed backfill required.
    _signup_login(client, "toan")
    resp = client.post("/v1/tts/profiles", json={"name": "p1", "engine": "omnivoice"})
    assert resp.status_code == 200
