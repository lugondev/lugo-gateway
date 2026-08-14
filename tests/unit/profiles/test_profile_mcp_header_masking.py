"""F1 (whole-branch review, feat/shared-profile-clone-only): sharing a profile
publishes mcp_servers[].headers unmasked.

_mask() (app/api/routes/profiles.py) used to mask only llm.api_key. A shared
profile is returned to EVERY caller by list_profiles/get_profile -- that's the
point of `shared` -- so an mcp_server's header VALUES (typically a bearer
token) went out verbatim to anyone who could list templates. Keys are kept
unmasked so the editor can still show which headers exist; only the values are
replaced.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.auth.users import user_store
from app.services.profiles.store import profile_store

import asyncio


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
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


_SERVER = {
    "name": "internal",
    "url": "https://internal.example/mcp",
    "headers": {"Authorization": "Bearer sk-real-secret-abc123", "X-Empty": ""},
}


def test_shared_profile_list_masks_header_values_for_another_user(client, _with_password):
    admin = TestClient(app)
    _as_user(admin, "admin")
    name = _rand("tpl")
    resp = admin.post(
        "/v1/profiles", json={"name": name, "shared": True, "mcp_servers": [_SERVER]}
    )
    assert resp.status_code == 200, resp.text

    _as_user(client, "user")
    listed = client.get("/v1/profiles").json()["data"][name]
    headers = listed["mcp_servers"][0]["headers"]
    # Keys survive -- the editor needs them to show which headers exist.
    assert set(headers.keys()) == {"Authorization", "X-Empty"}
    # Non-empty values are masked...
    assert headers["Authorization"] == "***"
    assert "sk-real-secret-abc123" not in str(listed)
    # ...an empty value has nothing to leak and is left as-is.
    assert headers["X-Empty"] == ""


def test_shared_profile_get_masks_header_values(client, _with_password):
    admin = TestClient(app)
    _as_user(admin, "admin")
    name = _rand("tpl")
    assert admin.post(
        "/v1/profiles", json={"name": name, "shared": True, "mcp_servers": [_SERVER]}
    ).status_code == 200

    _as_user(client, "user")
    got = client.get(f"/v1/profiles/{name}").json()["data"]
    assert got["mcp_servers"][0]["headers"]["Authorization"] == "***"


def test_masking_does_not_touch_the_stored_value(client, _with_password):
    """The mask is a read-side view only -- the real header value must still
    be there in the store (and thus still usable by _build_tool_registry)."""
    _as_user(client, "admin")
    name = _rand("tpl")
    assert client.post(
        "/v1/profiles", json={"name": name, "shared": True, "mcp_servers": [_SERVER]}
    ).status_code == 200

    stored = profile_store.get(name)
    assert stored.mcp_servers[0].headers["Authorization"] == "Bearer sk-real-secret-abc123"

    # And the admin's own GET response is masked too -- shared means the
    # admin who made it can no longer run it either (profile_usable), so
    # there is no "it's mine, show me the real value" carve-out.
    got = client.get(f"/v1/profiles/{name}").json()["data"]
    assert got["mcp_servers"][0]["headers"]["Authorization"] == "***"
