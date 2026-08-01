"""An MCP server row's `headers` carry that server's credentials (bearer
tokens, api keys). /v1/mcp is a USER prefix and every row is a template
(owner_id=None, created by an admin), so `_visible` is True for everyone --
which means an unmasked `headers` in the response body hands every logged-in
user the admin's MCP credentials. Every sibling secret surface already masks
(providers.py, model_registry.py, profiles.py); these two routes did not.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.auth.users import user_store
from app.services.mcp.models import McpServer
from app.services.mcp.server_store import mcp_server_store

SECRET = "Bearer super-secret-mcp-key-1234567890"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


@pytest.fixture
def secret_server():
    mcp_server_store.upsert(
        McpServer(
            name="secret-mcp",
            url="http://127.0.0.1:9/mcp",
            headers={"Authorization": SECRET},
            enabled=False,
            owner_id=None,
        )
    )
    yield
    mcp_server_store.delete("secret-mcp")


def _login(client, username, password, role="user"):
    client.post("/api/auth/signup", json={"username": username, "password": password})
    user = asyncio.run(user_store.get_by_username(username))
    if role == "admin":
        asyncio.run(user_store.set_fields(user.id, role="admin"))
    assert client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).status_code == 200


def test_list_servers_masks_header_values_for_a_plain_user(
    client, _with_password, secret_server
):
    _login(client, "plain-list", "s3cret", role="user")
    resp = client.get("/v1/mcp/servers")
    assert resp.status_code == 200
    assert SECRET not in resp.text
    assert resp.json()["data"]["secret-mcp"]["headers"] == {"Authorization": "***"}


def test_get_server_masks_header_values_for_a_plain_user(
    client, _with_password, secret_server
):
    _login(client, "plain-get", "s3cret", role="user")
    resp = client.get("/v1/mcp/servers/secret-mcp")
    assert resp.status_code == 200
    assert SECRET not in resp.text
    assert resp.json()["data"]["headers"] == {"Authorization": "***"}


def test_admin_still_sees_real_headers(client, _with_password, secret_server):
    """The admin UI round-trips headers through PUT /v1/mcp/servers/{name};
    masking them for the admin too would write '***' back into the row."""
    _login(client, "root-mcp", "s3cret", role="admin")
    resp = client.get("/v1/mcp/servers/secret-mcp")
    assert resp.status_code == 200
    assert resp.json()["data"]["headers"] == {"Authorization": SECRET}
