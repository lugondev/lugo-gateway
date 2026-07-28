"""The /llm routes mutate a SERVER-WIDE registry row. They must be admin-only
even though they live under the /v1/conversation user prefix."""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.auth.users import user_store


@pytest.fixture
def client():
    return TestClient(app)


def _as_user(client: TestClient, role: str) -> str:
    """Give `client` a logged-in session with the given role and return the
    new user's id. Signup+login (app/api/routes/auth.py) writes
    request.session["user_id"]/["role"] directly, so this works whether or
    not AuthGuardMiddleware is active (see tests/conftest.py's `_hermetic`)."""
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


def test_set_llm_config_rejected_for_normal_user(client):
    _as_user(client, "user")
    resp = client.post("/v1/conversation/llm", json={
        "base_url": "https://attacker.example/v1", "api_key": "x", "model": "gpt-4o",
    })
    assert resp.status_code == 403


def test_reset_llm_config_rejected_for_normal_user(client):
    _as_user(client, "user")
    assert client.post("/v1/conversation/llm/reset").status_code == 403


def test_get_llm_config_rejected_for_normal_user(client):
    """GET discloses the provider base_url -- admin-only too."""
    _as_user(client, "user")
    assert client.get("/v1/conversation/llm").status_code == 403


def test_admin_can_still_set_llm_config(client):
    _as_user(client, "admin")
    resp = client.post("/v1/conversation/llm", json={
        "base_url": "http://localhost:11434/v1", "api_key": "", "model": "llama3",
    })
    assert resp.status_code == 200


def test_admin_can_still_get_and_reset_llm_config(client):
    _as_user(client, "admin")
    assert client.get("/v1/conversation/llm").status_code == 200
    assert client.post("/v1/conversation/llm/reset").status_code == 200
