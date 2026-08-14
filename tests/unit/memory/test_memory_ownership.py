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


def test_reads_of_a_shared_template_are_allowed_but_always_empty(client, _with_password):
    """A shared template is visible (GET /v1/profiles already lists it to
    everyone), so reading its memory bucket is allowed and simply reflects
    reality: nobody can ever write to it (see the write-refusal tests below),
    so the bucket stays empty forever. Returning [] here is more honest than
    a 404."""
    _signup_login(client, "root", role="admin")
    client.post("/v1/profiles", json={"name": "template-a", "shared": True})

    _signup_login(client, "toan", role="user")
    resp = client.get(_mem_url("template-a"))
    assert resp.status_code == 200
    assert resp.json()["data"] == []


def test_writing_to_a_shared_template_is_refused_for_every_role(client, _with_password):
    """A shared profile is a clone-only template: runnable (and thus
    writable-to-memory) by nobody, not even the admin who created it -- the
    same asymmetry profile_usable() enforces for running/binding a device.
    Writing here used to succeed and land in the caller's own bucket, but the
    profile that name refers to can never run and a clone gets a different
    name, so those rows were permanently orphaned.

    Refused with a named 400, not the generic 404 -- same convention
    devices.py's _checked_profile_name already uses for binding a device to
    a shared template. GET /v1/profiles already lists this row to everyone,
    so naming it in the rejection leaks nothing, and it would be incoherent
    for GET .../memories/template-a to return 200 [] while POSTing the same
    name gets the same 404 as a name that doesn't exist. A private or
    missing profile is a different story -- see the 404 tests above, which
    that no-enumeration-oracle contract still requires."""
    _signup_login(client, "root", role="admin")
    client.post("/v1/profiles", json={"name": "template-a", "shared": True})
    expected_detail = "profile 'template-a' is a shared template; clone it before using it"

    # The creating admin gets no special pass -- shared is usable by nobody.
    resp = client.post(_mem_url("template-a"), json={"content": "root-note"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == expected_detail

    _signup_login(client, "toan", role="user")
    resp = client.post(_mem_url("template-a"), json={"content": "toan-note"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == expected_detail
    # No memory landed anywhere reachable through this name.
    assert client.get(_mem_url("template-a")).json()["data"] == []
