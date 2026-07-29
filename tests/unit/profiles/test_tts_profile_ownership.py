import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.tts.profile_store import TtsProfileStore


@pytest.fixture(autouse=True)
def _clean_store(tmp_path, monkeypatch):
    # Mirrors tests/unit/profiles/test_tts_profile_routes.py: tts_profile_store is a
    # module-level singleton with an in-memory cache that, once populated,
    # ignores the fresh per-test SQLite file the autouse tests/conftest.py
    # `_tmp_db` fixture points the engine at -- writes would silently target
    # a tableless DB. A brand new TtsProfileStore (cache=None) per test avoids
    # that staleness.
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


def _signup_login(client, username: str, role: str = "user") -> None:
    client.post("/api/auth/signup", json={"username": username, "password": "pw"})
    if role == "admin":
        import asyncio

        from app.services.auth.users import user_store

        user = asyncio.run(user_store.get_by_username(username))
        asyncio.run(user_store.set_fields(user.id, role="admin"))
    client.post("/api/auth/login", json={"username": username, "password": "pw"})


def test_admin_created_tts_profile_is_template(client, _with_password):
    _signup_login(client, "root", role="admin")
    resp = client.post("/v1/tts/profiles", json={"name": "shared-voice"})
    assert resp.json()["data"]["owner_id"] is None


def test_user_created_tts_profile_is_owned_and_others_hidden(client, _with_password):
    _signup_login(client, "a", role="user")
    client.post("/v1/tts/profiles", json={"name": "a-voice"})

    _signup_login(client, "b", role="user")
    resp = client.get("/v1/tts/profiles/a-voice")
    assert resp.status_code == 404
    assert "a-voice" not in client.get("/v1/tts/profiles").json()["data"]


def test_clone_tts_profile(client, _with_password):
    _signup_login(client, "root", role="admin")
    client.post("/v1/tts/profiles", json={"name": "template-voice"})

    _signup_login(client, "toan", role="user")
    resp = client.post("/v1/tts/profiles/template-voice/clone", json={"new_name": "toan-voice"})
    assert resp.status_code == 200
    assert resp.json()["data"]["owner_id"] is not None


def test_create_rejects_name_taken_by_another_users_private_profile(client, _with_password):
    _signup_login(client, "a", role="user")
    client.post("/v1/tts/profiles", json={"name": "a-secret-voice"})

    _signup_login(client, "b", role="user")
    resp = client.post("/v1/tts/profiles", json={"name": "a-secret-voice"})
    assert resp.status_code == 409
    # confirm a's row survived untouched
    _signup_login(client, "a", role="user")
    assert client.get("/v1/tts/profiles/a-secret-voice").status_code == 200


def test_regular_user_cannot_update_or_delete_admin_template(client, _with_password):
    _signup_login(client, "root", role="admin")
    client.post("/v1/tts/profiles", json={"name": "template-voice-2"})

    _signup_login(client, "mallory", role="user")
    resp = client.put("/v1/tts/profiles/template-voice-2", json={"name": "template-voice-2"})
    assert resp.status_code == 404
    resp = client.delete("/v1/tts/profiles/template-voice-2")
    assert resp.status_code == 404
    assert client.get("/v1/tts/profiles/template-voice-2").status_code == 200
