from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.mcp.client import McpConnectionError, McpHttpClient


def _make_mock_client(get_response=None, post_response=None):
    """Build a mock async context-manager httpx.AsyncClient."""
    mock = AsyncMock()
    if get_response is not None:
        mock.get = AsyncMock(return_value=get_response)
    if post_response is not None:
        mock.post = AsyncMock(return_value=post_response)
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=False)
    return mock


def _json_response(data, status=200):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=data)
    resp.status_code = status
    return resp


async def test_list_tools_list_format():
    tools = [{"name": "search", "description": "Search", "inputSchema": {"type": "object"}}]
    mock_client = _make_mock_client(get_response=_json_response(tools))
    with patch("httpx.AsyncClient", return_value=mock_client):
        client = McpHttpClient("http://mcp.test")
        result = await client.list_tools()
    assert len(result) == 1
    assert result[0]["name"] == "search"


async def test_list_tools_dict_format():
    tools = [{"name": "time", "description": "Get time", "inputSchema": {}}]
    mock_client = _make_mock_client(get_response=_json_response({"tools": tools}))
    with patch("httpx.AsyncClient", return_value=mock_client):
        client = McpHttpClient("http://mcp.test")
        result = await client.list_tools()
    assert result[0]["name"] == "time"


async def test_list_tools_strips_trailing_slash():
    mock_client = _make_mock_client(get_response=_json_response([]))
    with patch("httpx.AsyncClient", return_value=mock_client):
        client = McpHttpClient("http://mcp.test/")
        await client.list_tools()
    mock_client.get.assert_called_once_with("http://mcp.test/tools")


async def test_list_tools_connection_error_raises():
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    with patch("httpx.AsyncClient", return_value=mock_client):
        client = McpHttpClient("http://mcp.test")
        with pytest.raises(McpConnectionError, match="mcp.test"):
            await client.list_tools()


async def test_invoke_returns_result_string():
    mock_client = _make_mock_client(post_response=_json_response({"result": "found it"}))
    with patch("httpx.AsyncClient", return_value=mock_client):
        client = McpHttpClient("http://mcp.test")
        result = await client.invoke("search", {"query": "hello"})
    assert result == "found it"
    mock_client.post.assert_called_once_with(
        "http://mcp.test/tools/search", json={"arguments": {"query": "hello"}}
    )


async def test_list_tools_sends_custom_headers():
    mock_client = _make_mock_client(get_response=_json_response([]))
    with patch("httpx.AsyncClient", return_value=mock_client) as mock_ctor:
        client = McpHttpClient("http://mcp.test", headers={"X-API-Key": "secret"})
        await client.list_tools()
    assert mock_ctor.call_args.kwargs["headers"] == {"X-API-Key": "secret"}


async def test_invoke_sends_custom_headers():
    mock_client = _make_mock_client(post_response=_json_response({"result": "ok"}))
    with patch("httpx.AsyncClient", return_value=mock_client) as mock_ctor:
        client = McpHttpClient("http://mcp.test", headers={"Authorization": "Bearer x"})
        await client.invoke("search", {})
    assert mock_ctor.call_args.kwargs["headers"] == {"Authorization": "Bearer x"}


async def test_invoke_connection_error_raises():
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    with patch("httpx.AsyncClient", return_value=mock_client):
        client = McpHttpClient("http://mcp.test")
        with pytest.raises(McpConnectionError):
            await client.invoke("search", {})
