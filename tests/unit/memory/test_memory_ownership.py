import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.profiles.store import ProfileStore


@pytest.fixture(autouse=True)
def _clean_store(tmp_path, monkeypatch):
    # Mirrors tests/unit/profiles/test_profile_ownership.py: profile_store is a module-level
    # singleton whose in-memory cache would otherwise ignore the per-test SQLite
    # file. memories.py binds `profile_store` at import time, so patching the
    # services module alone would leave the route holding the stale singleton.
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    monkeypatch.setattr("app.api.routes.profiles.profile_store", fresh)
    monkeypatch.setattr("app.api.routes.memories.profile_store", fresh, raising=False)
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


def _mem_url(name: str) -> str:
    return f"/v1/profiles/{name}/memories"


def test_owner_can_add_and_list_own_memories(client, _with_password):
    _signup_login(client, "toan", role="user")
    client.post("/v1/profiles", json={"name": "mine"})

    resp = client.post(_mem_url("mine"), json={"content": "thich uong tra"})
    assert resp.status_code == 200

    resp = client.get(_mem_url("mine"))
    assert resp.status_code == 200
    assert [m["content"] for m in resp.json()["data"]] == ["thich uong tra"]


def test_listing_another_users_memories_is_404(client, _with_password):
    _signup_login(client, "a", role="user")
    client.post("/v1/profiles", json={"name": "a-private"})
    client.post(_mem_url("a-private"), json={"content": "bi mat cua a"})

    _signup_login(client, "b", role="user")
    assert client.get(_mem_url("a-private")).status_code == 404


def test_adding_to_another_users_profile_is_404(client, _with_password):
    _signup_login(client, "a", role="user")
    client.post("/v1/profiles", json={"name": "a-private"})

    _signup_login(client, "b", role="user")
    resp = client.post(_mem_url("a-private"), json={"content": "chen vao"})
    assert resp.status_code == 404

    # a's buffer must be untouched
    _signup_login(client, "a", role="user")
    assert client.get(_mem_url("a-private")).json()["data"] == []


def test_wiping_another_users_memories_is_404_and_preserves_them(client, _with_password):
    _signup_login(client, "a", role="user")
    client.post("/v1/profiles", json={"name": "a-private"})
    client.post(_mem_url("a-private"), json={"content": "dung xoa toi"})

    _signup_login(client, "mallory", role="user")
    assert client.delete(_mem_url("a-private")).status_code == 404

    _signup_login(client, "a", role="user")
    assert [m["content"] for m in client.get(_mem_url("a-private")).json()["data"]] == ["dung xoa toi"]


def test_updating_or_deleting_a_single_memory_of_another_user_is_404(client, _with_password):
    _signup_login(client, "a", role="user")
    client.post("/v1/profiles", json={"name": "a-private"})
    mem_id = client.post(_mem_url("a-private"), json={"content": "goc"}).json()["data"]["id"]

    _signup_login(client, "mallory", role="user")
    assert client.put(f"{_mem_url('a-private')}/{mem_id}", json={"content": "sua trom"}).status_code == 404
    assert client.delete(f"{_mem_url('a-private')}/{mem_id}").status_code == 404

    _signup_login(client, "a", role="user")
    assert [m["content"] for m in client.get(_mem_url("a-private")).json()["data"]] == ["goc"]


def test_memories_of_nonexistent_profile_is_404(client, _with_password):
    _signup_login(client, "toan", role="user")
    assert client.get(_mem_url("khong-ton-tai")).status_code == 404
    assert client.post(_mem_url("khong-ton-tai"), json={"content": "x"}).status_code == 404
    assert client.delete(_mem_url("khong-ton-tai")).status_code == 404


def test_user_can_manage_own_memories_on_template(client, _with_password):
    _signup_login(client, "root", role="admin")
    client.post("/v1/profiles", json={"name": "template-a"})

    _signup_login(client, "toan", role="user")
    assert client.get(_mem_url("template-a")).status_code == 200
    assert client.post(_mem_url("template-a"), json={"content": "toan-note"}).status_code == 200
    assert [m["content"] for m in client.get(_mem_url("template-a")).json()["data"]] == ["toan-note"]


def test_template_memories_are_isolated_per_user(client, _with_password):
    _signup_login(client, "root", role="admin")
    client.post("/v1/profiles", json={"name": "template-a"})

    _signup_login(client, "a", role="user")
    client.post(_mem_url("template-a"), json={"content": "a-note"})

    _signup_login(client, "b", role="user")
    assert client.get(_mem_url("template-a")).json()["data"] == []
    client.post(_mem_url("template-a"), json={"content": "b-note"})

    _signup_login(client, "a", role="user")
    assert [m["content"] for m in client.get(_mem_url("template-a")).json()["data"]] == ["a-note"]
