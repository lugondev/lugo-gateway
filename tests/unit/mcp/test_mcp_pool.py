import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from app.services.mcp.pool import McpConnectionPool


TOOLS = [{"name": "search", "description": "Search", "inputSchema": {"type": "object"}}]


def _mock_client(tools=None, invoke_result="ok"):
    client = AsyncMock()
    client.list_tools = AsyncMock(return_value=tools or TOOLS)
    client.invoke = AsyncMock(return_value=invoke_result)
    return client


async def test_get_tools_returns_tool_list():
    pool = McpConnectionPool(cache_ttl=60)
    mock = _mock_client()
    with patch("app.services.mcp.pool.McpHttpClient", return_value=mock):
        tools = await pool.get_tools("http://mcp.test")
    assert tools == TOOLS
    mock.list_tools.assert_called_once()


async def test_get_tools_caches_result():
    pool = McpConnectionPool(cache_ttl=60)
    mock = _mock_client()
    with patch("app.services.mcp.pool.McpHttpClient", return_value=mock):
        await pool.get_tools("http://mcp.test")
        await pool.get_tools("http://mcp.test")
    mock.list_tools.assert_called_once()  # cached on second call


async def test_get_tools_cache_expires():
    pool = McpConnectionPool(cache_ttl=0.01)  # 10ms TTL
    mock = _mock_client()
    with patch("app.services.mcp.pool.McpHttpClient", return_value=mock):
        await pool.get_tools("http://mcp.test")
        await asyncio.sleep(0.02)
        await pool.get_tools("http://mcp.test")
    assert mock.list_tools.call_count == 2


async def test_get_tools_returns_empty_on_error():
    from app.services.mcp.client import McpConnectionError
    pool = McpConnectionPool(cache_ttl=60)
    mock = AsyncMock()
    mock.list_tools = AsyncMock(side_effect=McpConnectionError("refused"))
    with patch("app.services.mcp.pool.McpHttpClient", return_value=mock):
        tools = await pool.get_tools("http://mcp.test")
    assert tools == []


async def test_invoke_returns_result():
    pool = McpConnectionPool(cache_ttl=60)
    mock = _mock_client(invoke_result="search result")
    with patch("app.services.mcp.pool.McpHttpClient", return_value=mock):
        result = await pool.invoke("http://mcp.test", "search", {"query": "hello"})
    assert result == "search result"
    mock.invoke.assert_called_once_with("search", {"query": "hello"})


async def test_get_tools_passes_headers_to_client():
    pool = McpConnectionPool(cache_ttl=60)
    mock = _mock_client()
    with patch("app.services.mcp.pool.McpHttpClient", return_value=mock) as mock_ctor:
        await pool.get_tools("http://mcp.test", headers={"X-API-Key": "k"})
    assert mock_ctor.call_args.kwargs["headers"] == {"X-API-Key": "k"}


async def test_invoke_passes_headers_to_client():
    pool = McpConnectionPool(cache_ttl=60)
    mock = _mock_client()
    with patch("app.services.mcp.pool.McpHttpClient", return_value=mock) as mock_ctor:
        await pool.invoke("http://mcp.test", "search", {}, headers={"Authorization": "Bearer x"})
    assert mock_ctor.call_args.kwargs["headers"] == {"Authorization": "Bearer x"}


async def test_invalidate_clears_cache():
    pool = McpConnectionPool(cache_ttl=60)
    mock = _mock_client()
    with patch("app.services.mcp.pool.McpHttpClient", return_value=mock):
        await pool.get_tools("http://mcp.test")
        pool.invalidate("http://mcp.test")
        await pool.get_tools("http://mcp.test")
    assert mock.list_tools.call_count == 2


async def test_different_urls_are_independent():
    pool = McpConnectionPool(cache_ttl=60)
    mock_a = _mock_client(tools=[{"name": "a"}])
    mock_b = _mock_client(tools=[{"name": "b"}])
    with patch("app.services.mcp.pool.McpHttpClient", side_effect=[mock_a, mock_b]):
        tools_a = await pool.get_tools("http://a.test")
        tools_b = await pool.get_tools("http://b.test")
    assert tools_a[0]["name"] == "a"
    assert tools_b[0]["name"] == "b"


async def test_same_url_different_headers_get_distinct_clients():
    """Two server rows can share a URL but carry different headers (e.g.
    different bearer tokens). Keying the client pool on URL alone would hand
    the second registration the first caller's already-connected client --
    silently reusing its headers/credentials (credential bleed). This would
    fail (mock_ctor called once, both calls sharing the same client) if
    _get_client keyed on url alone."""
    pool = McpConnectionPool(cache_ttl=60)
    mock_a = _mock_client()
    mock_b = _mock_client()
    with patch("app.services.mcp.pool.McpHttpClient", side_effect=[mock_a, mock_b]) as mock_ctor:
        client_a = pool._get_client("http://mcp.test", headers={"Authorization": "Bearer a"})
        client_b = pool._get_client("http://mcp.test", headers={"Authorization": "Bearer b"})
    assert client_a is mock_a
    assert client_b is mock_b
    assert client_a is not client_b
    assert mock_ctor.call_count == 2
    assert mock_ctor.call_args_list[0].kwargs["headers"] == {"Authorization": "Bearer a"}
    assert mock_ctor.call_args_list[1].kwargs["headers"] == {"Authorization": "Bearer b"}


async def test_same_url_different_headers_do_not_share_tool_cache():
    """The tool cache must be keyed the same way as the client pool -- a
    cache keyed on URL alone would return the first caller's tool list (and
    implicitly, its client/headers) to a second caller using different
    headers. This would fail (list_tools called once, tools_b == tools_a) if
    get_tools's cache keyed on url alone."""
    pool = McpConnectionPool(cache_ttl=60)
    mock_a = _mock_client(tools=[{"name": "a-only-tool"}])
    mock_b = _mock_client(tools=[{"name": "b-only-tool"}])
    with patch("app.services.mcp.pool.McpHttpClient", side_effect=[mock_a, mock_b]):
        tools_a = await pool.get_tools("http://mcp.test", headers={"Authorization": "Bearer a"})
        tools_b = await pool.get_tools("http://mcp.test", headers={"Authorization": "Bearer b"})
    assert tools_a[0]["name"] == "a-only-tool"
    assert tools_b[0]["name"] == "b-only-tool"
    mock_a.list_tools.assert_called_once()
    mock_b.list_tools.assert_called_once()


async def test_invalidate_clears_all_header_variants_for_a_url():
    """invalidate(url) is called by callers (routes/mcp.py) that only know
    the URL, not which header sets were used to populate the pool -- it must
    clear every header-variant client/cache entry for that URL, not just the
    no-headers one."""
    pool = McpConnectionPool(cache_ttl=60)
    mock_a = _mock_client()
    mock_b = _mock_client()
    with patch("app.services.mcp.pool.McpHttpClient", side_effect=[mock_a, mock_b, mock_a, mock_b]):
        await pool.get_tools("http://mcp.test", headers={"Authorization": "Bearer a"})
        await pool.get_tools("http://mcp.test", headers={"Authorization": "Bearer b"})
        pool.invalidate("http://mcp.test")
        await pool.get_tools("http://mcp.test", headers={"Authorization": "Bearer a"})
        await pool.get_tools("http://mcp.test", headers={"Authorization": "Bearer b"})
    assert mock_a.list_tools.call_count == 2
    assert mock_b.list_tools.call_count == 2


async def test_get_tools_invalidate_during_fetch_skips_cache_write():
    """invalidate() called while get_tools() is awaiting must not be undone."""
    pool = McpConnectionPool(cache_ttl=60)

    async def slow_list_tools():
        # Simulate the event loop yielding and invalidate() running
        pool.invalidate("http://mcp.test")
        return TOOLS

    mock = AsyncMock()
    mock.list_tools = slow_list_tools
    with patch("app.services.mcp.pool.McpHttpClient", return_value=mock):
        tools = await pool.get_tools("http://mcp.test")

    # Tools returned correctly
    assert tools == TOOLS
    # But cache was NOT written (invalidate won)
    assert "http://mcp.test" not in pool._cache
