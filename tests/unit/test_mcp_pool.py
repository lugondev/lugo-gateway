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
