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


def test_user_created_mcp_server_hidden_from_other_users(client, _with_password):
    _signup_login(client, "a", role="user")
    client.post(
        "/v1/mcp/servers",
        json={"name": "a-secret-server", "url": "https://a.example.com/mcp", "headers": {"X-Api-Key": "s3cr3t"}},
    )

    _signup_login(client, "b", role="user")
    assert "a-secret-server" not in client.get("/v1/mcp/servers").json()["data"]
    resp = client.get("/v1/mcp/servers/a-secret-server")
    assert resp.status_code == 404


def test_create_rejects_name_taken_by_another_users_private_server(client, _with_password):
    _signup_login(client, "a", role="user")
    client.post("/v1/mcp/servers", json={"name": "a-secret-server", "url": "https://a.example.com/mcp"})

    _signup_login(client, "b", role="user")
    resp = client.post("/v1/mcp/servers", json={"name": "a-secret-server", "url": "https://b.example.com/mcp"})
    assert resp.status_code == 409
    # confirm a's row (and its own url) survived untouched
    _signup_login(client, "a", role="user")
    got = client.get("/v1/mcp/servers/a-secret-server")
    assert got.status_code == 200
    assert got.json()["data"]["url"] == "https://a.example.com/mcp"


def test_regular_user_cannot_update_or_delete_admin_template(client, _with_password):
    _signup_login(client, "root", role="admin")
    client.post("/v1/mcp/servers", json={"name": "template-mcp-2", "url": "https://t.example.com/mcp"})

    _signup_login(client, "mallory", role="user")
    resp = client.put(
        "/v1/mcp/servers/template-mcp-2",
        json={"name": "template-mcp-2", "url": "https://mallory.example.com/mcp"},
    )
    assert resp.status_code == 404
    resp = client.delete("/v1/mcp/servers/template-mcp-2")
    assert resp.status_code == 404
    got = client.get("/v1/mcp/servers/template-mcp-2")
    assert got.status_code == 200
    assert got.json()["data"]["url"] == "https://t.example.com/mcp"


def test_clone_mcp_server(client, _with_password):
    _signup_login(client, "root", role="admin")
    client.post("/v1/mcp/servers", json={"name": "template-mcp", "url": "https://t.example.com/mcp"})

    _signup_login(client, "toan", role="user")
    resp = client.post("/v1/mcp/servers/template-mcp/clone", json={"new_name": "toan-mcp"})
    assert resp.status_code == 200
    assert resp.json()["data"]["owner_id"] is not None
