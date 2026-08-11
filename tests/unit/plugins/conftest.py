"""Authenticated clients for the plugin registry route tests.

Same mechanism test_auth_guard.py (_with_password + login) and
test_profile_mcp_gate.py (_as_user) already use: /v1/plugins/* only tells
admin from non-admin apart when settings.admin_password is actually set --
with it empty (the `_hermetic` default in tests/conftest.py), auth_guard is a
no-op and current_role() defaults every caller to "admin" (see
app/core/actor.py). _with_password below turns auth on for this whole
directory so admin_client/user_client are meaningfully different actors.
"""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.auth.users import user_store


@pytest.fixture(autouse=True)
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _as_user(client: TestClient, role: str) -> str:
    """Same helper as test_profile_mcp_gate.py / test_conversation_authz.py --
    duplicated locally so this directory has no import-order coupling to
    those modules."""
    username = f"{role}-{uuid.uuid4().hex[:10]}"
    password = "s3cret-password"
    signup = client.post("/api/auth/signup", json={"username": username, "password": password})
    assert signup.status_code == 200, signup.text
    if role == "admin":
        user = asyncio.run(user_store.get_by_username(username))
        asyncio.run(user_store.set_fields(user.id, role="admin"))
    login = client.post("/api/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200, login.text
    return asyncio.run(user_store.get_by_username(username)).id


@pytest.fixture
def admin_client() -> TestClient:
    client = TestClient(app)
    _as_user(client, "admin")
    return client


@pytest.fixture
def user_client() -> TestClient:
    client = TestClient(app)
    _as_user(client, "user")
    return client
