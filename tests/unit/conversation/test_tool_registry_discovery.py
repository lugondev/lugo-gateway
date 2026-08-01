"""Tool discovery must not decide how long a conversation takes to start.

_build_tool_registry runs between "socket accepted" and `session_started` -- the
user cannot speak until it returns. It used to await each configured MCP server
in turn, so N servers cost N round trips end to end, and a server that ACCEPTED
the connection but never answered held every new conversation open forever
(get_tools() only handles a server that refuses).
"""

import asyncio

import pytest

from app.services.conversation.session import _build_tool_registry
from app.services.mcp.models import McpServer

pytestmark = pytest.mark.asyncio


class _Profile:
    """Just enough of a Profile for _build_tool_registry."""

    def __init__(self, servers):
        self.mcp_servers = servers


def _server(name: str, url: str) -> McpServer:
    return McpServer(name=name, url=url, enabled=True)


async def test_servers_are_listed_concurrently(monkeypatch):
    async def slow_get_tools(url, headers=None):
        await asyncio.sleep(0.2)
        return [{"name": f"tool-{url[-1]}", "description": "", "inputSchema": {}}]

    monkeypatch.setattr("app.services.conversation.session.mcp_pool.get_tools", slow_get_tools)

    profile = _Profile([_server(f"s{i}", f"http://mcp.test/{i}") for i in range(4)])
    started = asyncio.get_running_loop().time()
    registry = await _build_tool_registry(profile)
    elapsed = asyncio.get_running_loop().time() - started

    assert registry is not None
    # Serialized this would be ~0.8s. Concurrent it is one server's worth.
    assert elapsed < 0.5


async def test_a_server_that_never_answers_does_not_block_the_session(monkeypatch):
    async def never(url, headers=None):
        await asyncio.sleep(3600)

    monkeypatch.setattr("app.services.conversation.session.mcp_pool.get_tools", never)
    monkeypatch.setattr(
        "app.services.conversation.session.settings.mcp_tool_discovery_timeout_seconds", 0.1
    )

    profile = _Profile([_server("hung", "http://mcp.test/hung")])
    started = asyncio.get_running_loop().time()
    registry = await _build_tool_registry(profile)
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 1.0
    # The session starts, just without that server's tools.
    assert registry is None or "tool-hung" not in registry._tools


async def test_one_failing_server_does_not_lose_the_others(monkeypatch):
    async def flaky(url, headers=None):
        if url.endswith("bad"):
            raise RuntimeError("boom")
        return [{"name": "good_tool", "description": "", "inputSchema": {}}]

    monkeypatch.setattr("app.services.conversation.session.mcp_pool.get_tools", flaky)

    profile = _Profile([_server("bad", "http://mcp.test/bad"), _server("ok", "http://mcp.test/ok")])
    registry = await _build_tool_registry(profile)

    assert registry is not None
    assert registry.get("good_tool") is not None
