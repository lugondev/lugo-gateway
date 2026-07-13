import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.profiles.store import ProfileStore


@pytest.fixture(autouse=True)
def _clean_store(tmp_path, monkeypatch):
    # Mirrors tests/unit/test_profiles_routes.py: profile_store is a module-level
    # singleton with an in-memory cache that, once populated, ignores the fresh
    # per-test SQLite file the autouse tests/conftest.py `_tmp_db` fixture points
    # the engine at -- writes would silently target a tableless DB. A brand new
    # ProfileStore (cache=None) per test avoids that staleness.
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
        import asyncio

        from app.services.auth.users import user_store

        user = asyncio.run(user_store.get_by_username(username))
        asyncio.run(user_store.set_fields(user.id, role="admin"))
    client.post("/api/auth/login", json={"username": username, "password": "pw"})


def _minimal_profile(name: str) -> dict:
    return {"name": name}


def test_admin_created_profile_is_a_template(client, _with_password):
    _signup_login(client, "root", role="admin")
    resp = client.post("/v1/profiles", json=_minimal_profile("shared"))
    assert resp.status_code == 200
    assert resp.json()["data"]["owner_id"] is None


def test_user_created_profile_is_owned(client, _with_password):
    _signup_login(client, "toan", role="user")
    resp = client.post("/v1/profiles", json=_minimal_profile("mine"))
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["owner_id"] is not None


def test_list_shows_templates_and_own_but_not_others(client, _with_password):
    _signup_login(client, "root", role="admin")
    client.post("/v1/profiles", json=_minimal_profile("template-a"))

    _signup_login(client, "a", role="user")
    client.post("/v1/profiles", json=_minimal_profile("a-private"))

    _signup_login(client, "b", role="user")
    client.post("/v1/profiles", json=_minimal_profile("b-private"))
    names = set(client.get("/v1/profiles").json()["data"].keys())
    assert names == {"template-a", "b-private"}  # sees the template + own, not a's


def test_get_other_users_private_profile_is_404(client, _with_password):
    _signup_login(client, "a", role="user")
    client.post("/v1/profiles", json=_minimal_profile("a-private"))

    _signup_login(client, "b", role="user")
    resp = client.get("/v1/profiles/a-private")
    assert resp.status_code == 404


def test_clone_template_creates_owned_copy(client, _with_password):
    _signup_login(client, "root", role="admin")
    client.post("/v1/profiles", json=_minimal_profile("template-a"))

    _signup_login(client, "toan", role="user")
    resp = client.post("/v1/profiles/template-a/clone", json={"new_name": "toan-copy"})
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["name"] == "toan-copy"
    assert body["owner_id"] is not None
    # confirm it is now independently listed/owned
    names = set(client.get("/v1/profiles").json()["data"].keys())
    assert "toan-copy" in names


def test_clone_nonexistent_or_invisible_is_404(client, _with_password):
    _signup_login(client, "a", role="user")
    client.post("/v1/profiles", json=_minimal_profile("a-private"))

    _signup_login(client, "b", role="user")
    resp = client.post("/v1/profiles/a-private/clone", json={"new_name": "steal"})
    assert resp.status_code == 404


def test_clone_name_collision_is_409(client, _with_password):
    _signup_login(client, "root", role="admin")
    client.post("/v1/profiles", json=_minimal_profile("template-a"))

    _signup_login(client, "toan", role="user")
    client.post("/v1/profiles", json=_minimal_profile("taken"))
    resp = client.post("/v1/profiles/template-a/clone", json={"new_name": "taken"})
    assert resp.status_code == 409


def test_create_rejects_name_taken_by_another_users_private_profile(client, _with_password):
    _signup_login(client, "a", role="user")
    client.post("/v1/profiles", json=_minimal_profile("secret"))

    _signup_login(client, "b", role="user")
    resp = client.post("/v1/profiles", json=_minimal_profile("secret"))
    assert resp.status_code == 409
    # confirm a's row survived untouched
    _signup_login(client, "a", role="user")
    assert client.get("/v1/profiles/secret").status_code == 200


def test_clone_rejects_new_name_taken_by_another_users_private_profile(client, _with_password):
    _signup_login(client, "root", role="admin")
    client.post("/v1/profiles", json=_minimal_profile("template-a"))

    _signup_login(client, "a", role="user")
    client.post("/v1/profiles", json=_minimal_profile("a-secret"))

    _signup_login(client, "b", role="user")
    resp = client.post("/v1/profiles/template-a/clone", json={"new_name": "a-secret"})
    assert resp.status_code == 409


def test_regular_user_cannot_update_or_delete_admin_template(client, _with_password):
    _signup_login(client, "root", role="admin")
    client.post("/v1/profiles", json=_minimal_profile("template-a"))

    _signup_login(client, "mallory", role="user")
    resp = client.put("/v1/profiles/template-a", json=_minimal_profile("template-a"))
    assert resp.status_code == 404
    resp = client.delete("/v1/profiles/template-a")
    assert resp.status_code == 404

    # confirm the template still exists and is unchanged, visible to everyone
    assert client.get("/v1/profiles/template-a").status_code == 200


def test_admin_can_update_and_delete_template(client, _with_password):
    _signup_login(client, "root", role="admin")
    client.post("/v1/profiles", json=_minimal_profile("template-b"))
    resp = client.put("/v1/profiles/template-b", json=_minimal_profile("template-b"))
    assert resp.status_code == 200
    resp = client.delete("/v1/profiles/template-b")
    assert resp.status_code == 200
