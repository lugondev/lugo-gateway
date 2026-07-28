"""MCP servers are fetched by the gateway and their responses returned to the
caller -- so an arbitrary user-supplied URL is an SSRF proxy.

Any logged-in non-admin could previously POST /v1/mcp/servers with an
arbitrary url + headers, then GET /v1/mcp/servers/{name}/tools to make the
gateway fetch it and return the response body to them (e.g. against
http://169.254.169.254). Fix: create/update/delete/clone become admin-only.
No IP blocklist -- the live basic-tools server self-hosts on loopback, which
is the normal deployment pattern here, and a blocklist only raises the bar
for an actor who must already be an admin. Read routes (list/get/tools) stay
open to normal users."""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.mcp.server_store import McpServerStore


@pytest.fixture(autouse=True)
def _clean_store(tmp_path, monkeypatch):
    fresh = McpServerStore(str(tmp_path / "mcp_servers.json"))
    monkeypatch.setattr("app.api.routes.mcp.mcp_server_store", fresh)
    monkeypatch.setattr("app.services.mcp.server_store.mcp_server_store", fresh)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    """tests/conftest.py's autouse `_hermetic` fixture blanks the admin
    passwords, making settings.auth_enabled False and short-circuiting the
    auth middleware (current_role() then falls back to "admin" for every
    caller). These tests need real role separation, so turn auth back on --
    same pattern as test_auth_guard_default_deny.py / test_conversation_authz.py."""
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _as_user(client: TestClient, role: str) -> str:
    username = f"{role}-{uuid.uuid4().hex[:10]}"
    password = "s3cret-password"
    signup = client.post("/api/auth/signup", json={"username": username, "password": password})
    assert signup.status_code == 200, signup.text
    if role == "admin":
        from app.services.auth.users import user_store

        user = asyncio.run(user_store.get_by_username(username))
        asyncio.run(user_store.set_fields(user.id, role="admin"))
    login = client.post("/api/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200, login.text
    from app.services.auth.users import user_store as _user_store

    return asyncio.run(_user_store.get_by_username(username)).id


def test_normal_user_cannot_create_mcp_server(client, _with_password):
    _as_user(client, "user")
    resp = client.post("/v1/mcp/servers", json={
        "name": "ssrf", "url": "http://169.254.169.254", "headers": {},
    })
    assert resp.status_code == 403
    assert resp.json()["detail"] == "admin only"


def test_normal_user_can_still_list_servers(client, _with_password):
    _as_user(client, "user")
    assert client.get("/v1/mcp/servers").status_code == 200


def test_normal_user_can_still_list_tools_of_a_visible_server(client, _with_password):
    """Read routes stay open to normal users -- only the write surface moves
    to admin-only."""
    _as_user(client, "admin")
    client.post("/v1/mcp/servers", json={"name": "basic-tools", "url": "http://localhost:8090"})

    from unittest.mock import AsyncMock

    _as_user(client, "user")
    mock_pool = AsyncMock()
    mock_pool.get_tools = AsyncMock(return_value=[])
    mock_pool.invalidate = lambda u: None
    import app.api.routes.mcp as mcp_routes

    orig_pool = mcp_routes.mcp_pool
    mcp_routes.mcp_pool = mock_pool
    try:
        resp = client.get("/v1/mcp/servers/basic-tools/tools")
        assert resp.status_code == 200
    finally:
        mcp_routes.mcp_pool = orig_pool


def test_normal_user_cannot_update_or_delete_or_clone(client, _with_password):
    """The whole write surface moves to admin, not just create."""
    _as_user(client, "admin")
    client.post("/v1/mcp/servers", json={"name": "basic-tools", "url": "http://localhost:8090"})

    _as_user(client, "user")
    resp = client.put("/v1/mcp/servers/basic-tools", json={
        "name": "basic-tools", "url": "http://attacker.example", "headers": {},
    })
    assert resp.status_code == 403
    assert resp.json()["detail"] == "admin only"

    resp = client.delete("/v1/mcp/servers/basic-tools")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "admin only"

    resp = client.post("/v1/mcp/servers/basic-tools/clone", json={"new_name": "copy"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "admin only"


def test_admin_can_still_create_a_loopback_server(client, _with_password):
    """Deliberate: self-hosted MCP on loopback is the normal pattern here --
    the live `basic-tools` server is at http://localhost:8090. No IP blocklist."""
    _as_user(client, "admin")
    resp = client.post("/v1/mcp/servers", json={
        "name": "ok", "url": "http://localhost:8090", "headers": {},
    })
    assert resp.status_code == 200, resp.text


def test_admin_can_still_update_delete_and_clone(client, _with_password):
    _as_user(client, "admin")
    client.post("/v1/mcp/servers", json={"name": "srv", "url": "http://localhost:8090"})

    resp = client.put("/v1/mcp/servers/srv", json={"name": "srv", "url": "http://localhost:8091"})
    assert resp.status_code == 200, resp.text

    resp = client.post("/v1/mcp/servers/srv/clone", json={"new_name": "srv-clone"})
    assert resp.status_code == 200, resp.text

    resp = client.delete("/v1/mcp/servers/srv-clone")
    assert resp.status_code == 200, resp.text


def test_ssrf_url_no_longer_reachable_end_to_end_for_a_normal_user(client, _with_password):
    """Full scenario from the brief: a non-admin can no longer make the
    gateway fetch and reflect back an arbitrary/internal URL at all, because
    they can never get a server pointed at one created in the first place."""
    _as_user(client, "user")
    create = client.post("/v1/mcp/servers", json={
        "name": "metadata-ssrf", "url": "http://169.254.169.254/latest/meta-data/", "headers": {},
    })
    assert create.status_code == 403
    # Nothing was created, so there is nothing to fetch tools from either.
    assert client.get("/v1/mcp/servers/metadata-ssrf/tools").status_code == 404
