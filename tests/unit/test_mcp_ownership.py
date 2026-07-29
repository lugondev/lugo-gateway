import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.mcp.server_store import McpServerStore


@pytest.fixture(autouse=True)
def _clean_store(tmp_path, monkeypatch):
    # Mirrors tests/unit/test_mcp_routes.py: mcp_server_store is a
    # module-level singleton with an in-memory cache that, once populated,
    # ignores the fresh per-test SQLite file the autouse tests/conftest.py
    # `_tmp_db` fixture points the engine at -- writes would silently target
    # a tableless DB. A brand new McpServerStore (cache=None) per test avoids
    # that staleness.
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


def test_regular_user_cannot_create_a_server_and_nothing_leaks(client, _with_password):
    """Post 2026-07-28-critical-authz-fixes task 6, create is admin-only, so
    a regular user can no longer produce a user-owned row at all -- the
    original scenario this test covered (two different non-admin owners) is
    no longer reachable via the API. This only proves the create attempt is
    correctly denied and that nothing leaked through as a result -- it is
    NOT a test of _visible()'s ownership-hiding branch (see
    test_clone_owner_id_hides_row_from_other_admins below for that; this
    test's previous name implied it covered that branch, but its old body
    never checked the POST's status code, so it was passing vacuously --
    the row was never created in the first place, so of course it wasn't
    visible to anyone)."""
    _signup_login(client, "a", role="user")
    resp = client.post(
        "/v1/mcp/servers",
        json={"name": "a-secret-server", "url": "https://a.example.com/mcp", "headers": {"X-Api-Key": "s3cr3t"}},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "admin only"

    _signup_login(client, "b", role="user")
    assert "a-secret-server" not in client.get("/v1/mcp/servers").json()["data"]
    resp = client.get("/v1/mcp/servers/a-secret-server")
    assert resp.status_code == 404


def test_clone_owner_id_hides_row_from_other_admins(client, _with_password):
    """_visible() (mcp.py) still has a live branch: clone_server sets
    owner_id=<cloning caller's user id> unconditionally (mcp.py:145), and
    since clone is now admin-only, that caller is always an admin -- so an
    admin's clone is invisible to every OTHER user, admin or not. Nothing
    in the suite exercised that until now (a prior grep for owner_id across
    tests/unit/test_mcp*.py found exactly one hit, and it only asserted
    non-null, not who it hides the row from)."""
    _signup_login(client, "root", role="admin")
    client.post("/v1/mcp/servers", json={"name": "template-mcp-3", "url": "https://t.example.com/mcp"})
    clone_resp = client.post("/v1/mcp/servers/template-mcp-3/clone", json={"new_name": "root-private-clone"})
    assert clone_resp.status_code == 200, clone_resp.text
    assert clone_resp.json()["data"]["owner_id"] is not None

    _signup_login(client, "other-admin", role="admin")
    assert "root-private-clone" not in client.get("/v1/mcp/servers").json()["data"]
    assert client.get("/v1/mcp/servers/root-private-clone").status_code == 404


def test_create_rejects_name_taken_by_an_existing_server(client, _with_password):
    """Create is admin-only now, so there is no longer a "taken by another
    USER's private server" scenario -- but the name-collision check itself
    (mcp_server_store.get(name) is not None -> 409) still applies regardless
    of who owns the existing row, so exercise it with two admins."""
    _signup_login(client, "a", role="admin")
    client.post("/v1/mcp/servers", json={"name": "a-secret-server", "url": "https://a.example.com/mcp"})

    _signup_login(client, "b", role="admin")
    resp = client.post("/v1/mcp/servers", json={"name": "a-secret-server", "url": "https://b.example.com/mcp"})
    assert resp.status_code == 409
    # confirm a's row (and its own url) survived untouched
    _signup_login(client, "a", role="admin")
    got = client.get("/v1/mcp/servers/a-secret-server")
    assert got.status_code == 200
    assert got.json()["data"]["url"] == "https://a.example.com/mcp"


def test_regular_user_cannot_update_or_delete_admin_template(client, _with_password):
    """Pre-task-6 this was a 404 (ownership check hid the target's
    existence); post-task-6 the admin gate runs first and denies with 403
    uniformly, before any ownership/existence lookup -- see
    tests/unit/test_mcp_ssrf.py for the dedicated SSRF-focused coverage of
    this same gate."""
    _signup_login(client, "root", role="admin")
    client.post("/v1/mcp/servers", json={"name": "template-mcp-2", "url": "https://t.example.com/mcp"})

    _signup_login(client, "mallory", role="user")
    resp = client.put(
        "/v1/mcp/servers/template-mcp-2",
        json={"name": "template-mcp-2", "url": "https://mallory.example.com/mcp"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "admin only"
    resp = client.delete("/v1/mcp/servers/template-mcp-2")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "admin only"
    got = client.get("/v1/mcp/servers/template-mcp-2")
    assert got.status_code == 200
    assert got.json()["data"]["url"] == "https://t.example.com/mcp"


def test_clone_mcp_server(client, _with_password):
    """Clone moved to admin-only in task 6 -- a regular user cloning a
    template (the original scenario here) is now a 403, covered in
    tests/unit/test_mcp_ssrf.py. This exercises the still-legitimate path:
    an admin cloning a template gets their own private copy."""
    _signup_login(client, "root", role="admin")
    client.post("/v1/mcp/servers", json={"name": "template-mcp", "url": "https://t.example.com/mcp"})

    _signup_login(client, "root2", role="admin")
    resp = client.post("/v1/mcp/servers/template-mcp/clone", json={"new_name": "root2-mcp"})
    assert resp.status_code == 200
    assert resp.json()["data"]["owner_id"] is not None
