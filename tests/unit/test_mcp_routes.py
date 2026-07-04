from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.mcp.models import McpServer
from app.services.mcp.server_store import McpServerStore, mcp_server_store


@pytest.fixture(autouse=True)
def _clean_store(tmp_path, monkeypatch):
    fresh = McpServerStore(str(tmp_path / "mcp.json"))
    monkeypatch.setattr("app.api.routes.mcp.mcp_server_store", fresh)
    monkeypatch.setattr("app.services.mcp.server_store.mcp_server_store", fresh)


@pytest.fixture
def client():
    return TestClient(app)


def test_list_servers_empty(client):
    resp = client.get("/v1/mcp/servers")
    assert resp.status_code == 200
    assert resp.json()["data"] == {}


def test_add_server(client):
    resp = client.post("/v1/mcp/servers", json={"name": "fs", "url": "http://localhost:3002/mcp"})
    assert resp.status_code == 200
    assert resp.json()["data"]["name"] == "fs"


def test_add_server_with_headers(client):
    resp = client.post(
        "/v1/mcp/servers",
        json={"name": "fs", "url": "http://localhost:3002/mcp", "headers": {"X-API-Key": "secret"}},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["headers"] == {"X-API-Key": "secret"}


def test_get_server(client):
    client.post("/v1/mcp/servers", json={"name": "ws", "url": "http://ws"})
    resp = client.get("/v1/mcp/servers/ws")
    assert resp.status_code == 200
    assert resp.json()["data"]["url"] == "http://ws"


def test_get_missing_server_404(client):
    assert client.get("/v1/mcp/servers/ghost").status_code == 404


def test_update_server_invalidates_cache(client, monkeypatch):
    invalidated = []
    monkeypatch.setattr("app.api.routes.mcp.mcp_pool.invalidate", lambda u: invalidated.append(u))
    client.post("/v1/mcp/servers", json={"name": "x", "url": "http://old"})
    client.put("/v1/mcp/servers/x", json={"name": "x", "url": "http://new"})
    assert "http://old" in invalidated


def test_delete_server(client):
    client.post("/v1/mcp/servers", json={"name": "del", "url": "http://del"})
    resp = client.delete("/v1/mcp/servers/del")
    assert resp.status_code == 200
    assert resp.json()["data"]["deleted"] is True
    assert client.get("/v1/mcp/servers/del").status_code == 404


def test_new_server_defaults_enabled_true(client):
    resp = client.post("/v1/mcp/servers", json={"name": "fs", "url": "http://localhost"})
    assert resp.json()["data"]["enabled"] is True


def test_set_enabled_false(client):
    client.post("/v1/mcp/servers", json={"name": "fs", "url": "http://localhost"})
    resp = client.patch("/v1/mcp/servers/fs/enabled", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["data"]["enabled"] is False
    assert client.get("/v1/mcp/servers/fs").json()["data"]["enabled"] is False


def test_set_enabled_missing_server_404(client):
    resp = client.patch("/v1/mcp/servers/ghost/enabled", json={"enabled": True})
    assert resp.status_code == 404


def test_delete_preset_server_blocked(client):
    client.post("/v1/mcp/servers", json={"name": "basic-tools", "url": "http://localhost:8090"})
    resp = client.delete("/v1/mcp/servers/basic-tools")
    assert resp.status_code == 400
    assert client.get("/v1/mcp/servers/basic-tools").status_code == 200


def test_list_server_tools(client, monkeypatch):
    client.post("/v1/mcp/servers", json={"name": "tool-srv", "url": "http://tool-srv"})
    tools = [{"name": "search", "description": "Search"}]
    mock_pool = AsyncMock()
    mock_pool.get_tools = AsyncMock(return_value=tools)
    mock_pool.invalidate = lambda u: None
    monkeypatch.setattr("app.api.routes.mcp.mcp_pool", mock_pool)
    resp = client.get("/v1/mcp/servers/tool-srv/tools")
    assert resp.status_code == 200
    assert resp.json()["data"]["tools"] == tools


def test_list_server_tools_passes_headers(client, monkeypatch):
    client.post(
        "/v1/mcp/servers",
        json={"name": "tool-srv", "url": "http://tool-srv", "headers": {"X-API-Key": "secret"}},
    )
    mock_pool = AsyncMock()
    mock_pool.get_tools = AsyncMock(return_value=[])
    mock_pool.invalidate = lambda u: None
    monkeypatch.setattr("app.api.routes.mcp.mcp_pool", mock_pool)
    client.get("/v1/mcp/servers/tool-srv/tools")
    mock_pool.get_tools.assert_called_once_with("http://tool-srv", headers={"X-API-Key": "secret"})


def test_list_tools_missing_server_404(client):
    resp = client.get("/v1/mcp/servers/ghost/tools")
    assert resp.status_code == 404
