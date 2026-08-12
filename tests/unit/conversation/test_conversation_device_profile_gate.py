"""A paired device (services.auth.devices) with no profile_id bound must be
refused on /v1/conversation/stream too, mirroring lugo.py's gate -- see
docs/superpowers/specs/2026-08-12-device-profile-pairing-admin-ui-design.md.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.auth.devices import device_store
from app.services.auth.users import user_store
from app.services.profiles.models import Profile
from app.services.profiles.store import profile_store


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    """Same rationale as test_conversation_authz.py's fixture of the same
    name: the autouse `_hermetic` fixture in conftest.py blanks the admin
    password, which makes settings.auth_enabled False and short-circuits
    resolve_ws_identity to an unscoped unauthenticated=True identity that
    can never be via_device -- these tests need a real device-token identity."""
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _as_user(client: TestClient) -> str:
    username = f"gate-conv-{__import__('uuid').uuid4().hex[:10]}"
    password = "s3cret-password"
    signup = client.post("/api/auth/signup", json={"username": username, "password": password})
    assert signup.status_code == 200, signup.text
    login = client.post("/api/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200, login.text
    return asyncio.run(user_store.get_by_username(username)).id


def test_unbound_paired_device_is_refused(client, _with_password):
    user_id = _as_user(client)
    _device, raw_token = asyncio.run(
        device_store.create(user_id, "ESP32", "AA:BB:GATE3")
    )

    device_client = TestClient(app)
    with device_client.websocket_connect(
        f"/v1/conversation/stream?output=text&device_token={raw_token}"
    ) as ws:
        msg = ws.receive_json()
        assert msg["event"] == "error"
        assert "profile" in msg["message"]


def test_bound_paired_device_still_connects(client, _with_password):
    user_id = _as_user(client)
    profile_store.upsert(Profile(name="conv-bound-profile", owner_id=user_id))
    _device, raw_token = asyncio.run(
        device_store.create(
            user_id, "ESP32", "AA:BB:GATE4", profile_id="conv-bound-profile"
        )
    )

    device_client = TestClient(app)
    with device_client.websocket_connect(
        f"/v1/conversation/stream?output=text&device_token={raw_token}"
    ) as ws:
        msg = ws.receive_json()
        assert msg["event"] == "session_started"
