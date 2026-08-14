"""CRUD half of clone-only shared profiles.

The load-bearing change is in create: `owner_id = None if is_admin` used to
mean an admin could not own a profile at all -- every one they made was a
template. Ownership and sharing are now independent, so an admin gets a normal
owned profile unless they explicitly ask for a shared one.
"""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.auth.devices import device_store
from app.services.auth.users import user_store
from app.services.profiles.store import profile_store


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    """tests/conftest.py's autouse `_hermetic` blanks the admin password, which
    turns auth off entirely. These tests need real roles -- same pattern as
    test_profile_idor.py."""
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _as_user(client: TestClient, role: str = "user") -> str:
    username = f"{role}-{uuid.uuid4().hex[:10]}"
    password = "s3cret-password"
    assert client.post(
        "/api/auth/signup", json={"username": username, "password": password}
    ).status_code == 200
    if role == "admin":
        user = asyncio.run(user_store.get_by_username(username))
        asyncio.run(user_store.set_fields(user.id, role="admin"))
    assert client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).status_code == 200
    return asyncio.run(user_store.get_by_username(username)).id


def _rand(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def test_admin_create_is_owned_not_shared_by_default(client, _with_password):
    admin_id = _as_user(client, "admin")
    name = _rand("adm")
    resp = client.post("/v1/profiles", json={"name": name})
    assert resp.status_code == 200, resp.text
    row = profile_store.get(name)
    assert row.owner_id == admin_id, "admin profiles used to be ownerless templates"
    assert row.shared is False


def test_admin_can_create_a_shared_template(client, _with_password):
    admin_id = _as_user(client, "admin")
    name = _rand("tpl")
    assert client.post("/v1/profiles", json={"name": name, "shared": True}).status_code == 200
    row = profile_store.get(name)
    assert row.shared is True
    assert row.owner_id == admin_id, "a shared row still records who made it"


def test_non_admin_cannot_create_a_shared_profile(client, _with_password):
    _as_user(client, "user")
    name = _rand("usr")
    # Silently dropped, not 403 -- same contract as mcp_servers (profiles.py),
    # so the profile editor needs no new error path.
    assert client.post("/v1/profiles", json={"name": name, "shared": True}).status_code == 200
    assert profile_store.get(name).shared is False


def test_non_admin_cannot_flip_shared_on_their_own_profile(client, _with_password):
    _as_user(client, "user")
    name = _rand("usr")
    assert client.post("/v1/profiles", json={"name": name}).status_code == 200
    assert client.put(f"/v1/profiles/{name}", json={"name": name, "shared": True}).status_code == 200
    assert profile_store.get(name).shared is False


def test_admin_update_can_flip_shared_both_ways(client, _with_password):
    _as_user(client, "admin")
    name = _rand("adm")
    assert client.post("/v1/profiles", json={"name": name}).status_code == 200
    assert client.put(f"/v1/profiles/{name}", json={"name": name, "shared": True}).status_code == 200
    assert profile_store.get(name).shared is True
    assert client.put(f"/v1/profiles/{name}", json={"name": name, "shared": False}).status_code == 200
    assert profile_store.get(name).shared is False


def test_update_preserves_shared_when_payload_omits_it(client, _with_password):
    """ProfileRequest.shared defaults to False, so an admin editing an unrelated
    field with a client that predates this feature must not silently un-share."""
    _as_user(client, "admin")
    name = _rand("adm")
    assert client.post("/v1/profiles", json={"name": name, "shared": True}).status_code == 200
    assert client.put(
        f"/v1/profiles/{name}", json={"name": name, "nickname": "renamed"}
    ).status_code == 200
    row = profile_store.get(name)
    assert row.nickname == "renamed"
    assert row.shared is True


def test_clone_of_a_shared_template_is_owned_and_not_shared(client, _with_password):
    admin = TestClient(app)
    _as_user(admin, "admin")
    tpl = _rand("tpl")
    assert admin.post("/v1/profiles", json={"name": tpl, "shared": True}).status_code == 200

    user_id = _as_user(client, "user")
    copy = _rand("copy")
    resp = client.post(f"/v1/profiles/{tpl}/clone", json={"new_name": copy})
    assert resp.status_code == 200, resp.text
    row = profile_store.get(copy)
    assert row.owner_id == user_id
    assert row.shared is False, "a clone is a working profile, never another template"


def test_sharing_a_profile_with_bound_devices_is_refused(client, _with_password):
    """Otherwise the admin creates exactly the state this feature exists to
    prevent: a device bound to a profile it is no longer allowed to run, which
    would silently degrade to server defaults on its next connect."""
    user_id = _as_user(client, "admin")
    name = _rand("bound")
    assert client.post("/v1/profiles", json={"name": name}).status_code == 200
    asyncio.run(device_store.create(user_id, "speaker", _rand("serial"), profile_id=name))
    device_id = asyncio.run(device_store.list_for_user(user_id))[0]["id"]

    resp = client.put(f"/v1/profiles/{name}", json={"name": name, "shared": True})
    assert resp.status_code == 409, resp.text
    assert device_id in resp.json()["detail"], "the admin needs to know WHICH devices to fix"
    assert profile_store.get(name).shared is False


def test_unsharing_is_never_blocked_by_devices(client, _with_password):
    """The 409 guards one direction only -- going back to a private profile
    creates no dangling binding."""
    _as_user(client, "admin")
    name = _rand("tpl")
    assert client.post("/v1/profiles", json={"name": name, "shared": True}).status_code == 200
    assert client.put(f"/v1/profiles/{name}", json={"name": name, "shared": False}).status_code == 200
    assert profile_store.get(name).shared is False
