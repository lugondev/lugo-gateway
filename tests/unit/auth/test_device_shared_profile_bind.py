"""Binding is the one shared-profile rejection that is NOT a silent fallback.

The WS/chat paths fall back to server defaults with a warning because a bad
`?profile=` should not brick a speaker. Binding is a deliberate admin action
with a form behind it, so it fails loudly instead -- and, since a shared
profile is listed to every caller by GET /v1/profiles, it may be named.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.auth.devices import device_store
from app.services.auth.users import user_store


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
    import uuid

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
    import uuid

    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def test_reassign_to_a_shared_profile_is_rejected(client, _with_password):
    user_id = _as_user(client, "admin")
    tpl = _rand("tpl")
    assert client.post("/v1/profiles", json={"name": tpl, "shared": True}).status_code == 200
    asyncio.run(device_store.create(user_id, "speaker", _rand("serial"), profile_id=""))
    device_id = asyncio.run(device_store.list_for_user(user_id))[0]["id"]

    resp = client.post(f"/v1/devices/mine/{device_id}/profile", json={"profile_id": tpl})
    assert resp.status_code == 400, resp.text
    assert (
        resp.json()["detail"]
        == f"profile '{tpl}' is a shared template; clone it before using it"
    )
    assert asyncio.run(device_store.get_by_id(device_id)).profile_id == ""


def test_reassign_to_someone_elses_private_profile_still_404s(client, _with_password):
    """Unchanged: the private-profile path must stay a 404, and -- the actual
    invariant, not just its symptom -- indistinguishable from naming a
    profile that was never created at all: same status, same detail text
    once the caller-known name is normalized out of each. Same normalization
    shape as tests/unit/profiles/test_profile_idor.py's
    test_ws_private_profile_falls_back_like_nonexistent."""
    alice = TestClient(app)
    _as_user(alice, "user")
    private = _rand("priv")
    assert alice.post("/v1/profiles", json={"name": private}).status_code == 200

    user_id = _as_user(client, "user")
    asyncio.run(device_store.create(user_id, "speaker", _rand("serial"), profile_id=""))
    device_id = asyncio.run(device_store.list_for_user(user_id))[0]["id"]

    resp_victim = client.post(
        f"/v1/devices/mine/{device_id}/profile", json={"profile_id": private}
    )
    nonexistent = _rand("ghost")
    resp_ghost = client.post(
        f"/v1/devices/mine/{device_id}/profile", json={"profile_id": nonexistent}
    )

    assert resp_victim.status_code == resp_ghost.status_code == 404
    detail_victim = resp_victim.json()["detail"]
    detail_ghost = resp_ghost.json()["detail"]
    assert "shared" not in detail_victim
    assert detail_victim.replace(private, "<NAME>") == detail_ghost.replace(
        nonexistent, "<NAME>"
    )


def test_unassigning_still_works(client, _with_password):
    user_id = _as_user(client, "user")
    name = _rand("own")
    assert client.post("/v1/profiles", json={"name": name}).status_code == 200
    asyncio.run(device_store.create(user_id, "speaker", _rand("serial"), profile_id=name))
    device_id = asyncio.run(device_store.list_for_user(user_id))[0]["id"]

    assert client.post(
        f"/v1/devices/mine/{device_id}/profile", json={"profile_id": ""}
    ).status_code == 200
    assert asyncio.run(device_store.get_by_id(device_id)).profile_id == ""
