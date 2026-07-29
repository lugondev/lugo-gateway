"""Adversarial regression for Task 7 (M4 + carried Task 4 follow-up).

M4: `PATCH /v1/mcp/servers/{name}/enabled` was the one mutating MCP route
Task 6 missed -- it only ran `_can_write`, so a user-owned row's own owner
could re-enable it, and `_build_tool_registry` reads mcp_server_store.list()
unfiltered, injecting that row's tools into EVERY user's conversation. Fixed
by adding `_require_admin` as the first line, matching create/update/delete/
clone.

Carried from Task 4 (same class as H4): mcp.py's create and clone still used
`mcp_server_store.get(name) is not None` for their 409 check, so an
unreadable row (get() returns None, but the name is occupied) stayed
claimable/overwritable. Fixed by switching both checks to the base-class
`exists()` method (see test_config_store_claimable.py for the original H4
fix and its `_write_raw_mcp_row` pattern, reused here).
"""
import json

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.db.config_models import McpServerRow
from app.services.db.sync_engine import init_config_tables, session_scope
from app.services.mcp.server_store import McpServerStore


def _write_raw_mcp_row(name: str, data: str) -> None:
    """Straight to the config_mcp_servers table, bypassing
    McpServerStore.upsert() (and McpServer's own validation) entirely --
    same pattern as test_config_store_claimable.py."""
    init_config_tables()
    with session_scope() as s:
        s.merge(McpServerRow(name=name, data=data))


def _read_raw_mcp_row(name: str) -> str | None:
    with session_scope() as s:
        row = s.get(McpServerRow, name)
        return row.data if row is not None else None


MALFORMED_MCP_DATA = json.dumps({
    "name": "victim-mcp", "owner_id": None, "url": "ftp://localhost:8090",
    "headers": {}, "enabled": False,
})


@pytest.fixture(autouse=True)
def _clean_store(tmp_path, monkeypatch):
    # mcp_server_store is a module-level singleton with an in-memory cache
    # that, once populated, ignores the fresh per-test SQLite file the
    # autouse tests/conftest.py `_tmp_db` fixture points the engine at.
    fresh = McpServerStore(str(tmp_path / "mcp_servers.json"))
    monkeypatch.setattr("app.api.routes.mcp.mcp_server_store", fresh)
    monkeypatch.setattr("app.services.mcp.server_store.mcp_server_store", fresh)


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


# ---------------------------------------------------------------------------
# M4: PATCH .../enabled is admin-only.
# ---------------------------------------------------------------------------


def test_non_admin_cannot_toggle_enabled_on_a_template(client, _with_password):
    _signup_login(client, "root", role="admin")
    resp = client.post(
        "/v1/mcp/servers", json={"name": "template-enabled-gate", "url": "https://t.example.com/mcp"}
    )
    assert resp.status_code == 200, resp.text

    _signup_login(client, "mallory", role="user")
    resp = client.patch("/v1/mcp/servers/template-enabled-gate/enabled", json={"enabled": False})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "admin only"

    # Confirm nothing changed.
    _signup_login(client, "root", role="admin")
    got = client.get("/v1/mcp/servers/template-enabled-gate")
    assert got.json()["data"]["enabled"] is True


def test_admin_can_toggle_enabled(client, _with_password):
    _signup_login(client, "root", role="admin")
    client.post("/v1/mcp/servers", json={"name": "template-enabled-gate-2", "url": "https://t.example.com/mcp"})

    resp = client.patch("/v1/mcp/servers/template-enabled-gate-2/enabled", json={"enabled": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["enabled"] is False
    assert client.get("/v1/mcp/servers/template-enabled-gate-2").json()["data"]["enabled"] is False


def test_owner_of_a_legacy_user_owned_row_still_cannot_toggle_it(client, monkeypatch, _with_password):
    """Even a legacy row's own owner (the exact M4 scenario) is now denied --
    _can_write alone used to let this through."""
    from app.services.mcp.models import McpServer

    _signup_login(client, "mallory", role="user")
    import asyncio

    from app.services.auth.users import user_store

    mallory = asyncio.run(user_store.get_by_username("mallory"))

    from app.api.routes.mcp import mcp_server_store

    mcp_server_store.upsert(
        McpServer(
            name="mallory-legacy-mcp", owner_id=mallory.id,
            url="https://mallory.example.com/mcp", enabled=False,
        )
    )

    resp = client.patch("/v1/mcp/servers/mallory-legacy-mcp/enabled", json={"enabled": True})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "admin only"


# ---------------------------------------------------------------------------
# Carried Task 4: create/clone must not claim/overwrite an unreadable row.
# ---------------------------------------------------------------------------


def test_create_over_unreadable_row_is_409_not_overwrite(client, _with_password):
    _write_raw_mcp_row("victim-mcp", MALFORMED_MCP_DATA)

    _signup_login(client, "root", role="admin")
    resp = client.post("/v1/mcp/servers", json={"name": "victim-mcp", "url": "https://attacker.example.com/mcp"})

    assert resp.status_code == 409
    # The raw row is byte-identical afterwards -- NOT overwritten with the
    # attacker's data.
    assert _read_raw_mcp_row("victim-mcp") == MALFORMED_MCP_DATA


def test_clone_over_unreadable_new_name_is_409_not_overwrite(client, _with_password):
    # Written BEFORE any store access so it lands in the store's first
    # _ensure() cache build (and thus its `_unreadable` set) -- writing raw
    # rows after the cache is already primed wouldn't be picked up until the
    # next full reload, same caveat as test_config_store_claimable.py.
    _write_raw_mcp_row("clone-target-mcp", MALFORMED_MCP_DATA)

    _signup_login(client, "root", role="admin")
    resp = client.post("/v1/mcp/servers", json={"name": "clone-source-mcp", "url": "https://t.example.com/mcp"})
    assert resp.status_code == 200, resp.text

    resp = client.post("/v1/mcp/servers/clone-source-mcp/clone", json={"new_name": "clone-target-mcp"})
    assert resp.status_code == 409
    assert _read_raw_mcp_row("clone-target-mcp") == MALFORMED_MCP_DATA


def test_create_genuinely_free_name_still_works(client, _with_password):
    _signup_login(client, "root", role="admin")
    resp = client.post("/v1/mcp/servers", json={"name": "brand-new-mcp", "url": "https://t.example.com/mcp"})
    assert resp.status_code == 200
    assert resp.json()["data"]["name"] == "brand-new-mcp"
