import pytest

from app.services.mcp.models import McpServer
from app.services.mcp.server_store import McpServerStore


@pytest.fixture
def store(tmp_path):
    return McpServerStore(str(tmp_path / "mcp.json"))


def test_empty_store(store):
    assert store.list() == {}


def test_upsert_and_get(store):
    s = McpServer(name="fs", url="http://localhost:3002/mcp")
    store.upsert(s)
    result = store.get("fs")
    assert result is not None
    assert result.url == "http://localhost:3002/mcp"


def test_get_missing_returns_none(store):
    assert store.get("ghost") is None


def test_list_multiple(store):
    store.upsert(McpServer(name="a", url="http://a"))
    store.upsert(McpServer(name="b", url="http://b"))
    assert set(store.list().keys()) == {"a", "b"}


def test_upsert_overwrites(store):
    store.upsert(McpServer(name="x", url="http://old"))
    store.upsert(McpServer(name="x", url="http://new"))
    assert store.get("x").url == "http://new"


def test_delete(store):
    store.upsert(McpServer(name="del", url="http://del"))
    store.delete("del")
    assert store.get("del") is None


def test_delete_nonexistent_noop(store):
    store.delete("ghost")


def test_persists_across_instances(tmp_path):
    path = str(tmp_path / "mcp.json")
    s1 = McpServerStore(path)
    s1.upsert(McpServer(name="p", url="http://persist"))
    s2 = McpServerStore(path)
    assert s2.get("p").url == "http://persist"
