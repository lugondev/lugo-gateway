from __future__ import annotations

import asyncio
import logging
import time

from app.core.settings import settings
from app.services.mcp.client import McpConnectionError, McpHttpClient

logger = logging.getLogger(__name__)


_PoolKey = tuple[str, frozenset[tuple[str, str]]]


class McpConnectionPool:
    """Lazy-connecting pool of MCP HTTP clients, one per (URL, headers).

    Two server rows can share a URL but carry different headers (e.g.
    different bearer tokens) -- keying on URL alone would hand the second
    caller's request the first caller's client, silently reusing its
    headers/credentials. Clients and the tool cache are therefore keyed on
    ``(url, frozenset(headers.items()))`` so distinct header sets never
    collide. Call ``invalidate(url)`` when a server's config changes to
    force a fresh fetch -- it clears every header-variant cached for that
    URL, since the caller may not know which headers were previously used.
    """

    def __init__(
        self,
        cache_ttl: float = 300.0,
        connect_timeout: float = 10.0,
        tool_timeout: float = 30.0,
    ) -> None:
        self._clients: dict[_PoolKey, McpHttpClient] = {}
        self._cache: dict[_PoolKey, tuple[float, list[dict]]] = {}  # key -> (timestamp, tools)
        self._ttl = cache_ttl
        self._connect_timeout = connect_timeout
        self._tool_timeout = tool_timeout
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(url: str, headers: dict[str, str] | None) -> _PoolKey:
        return (url, frozenset((headers or {}).items()))

    def _get_client(self, url: str, headers: dict[str, str] | None = None) -> McpHttpClient:
        key = self._key(url, headers)
        if key not in self._clients:
            self._clients[key] = McpHttpClient(
                url, self._connect_timeout, self._tool_timeout, headers=headers
            )
        return self._clients[key]

    async def get_tools(self, url: str, headers: dict[str, str] | None = None) -> list[dict]:
        key = self._key(url, headers)
        async with self._lock:
            cached = self._cache.get(key)
            if cached and (time.monotonic() - cached[0]) < self._ttl:
                return cached[1]
            client = self._get_client(url, headers)

        try:
            tools = await client.list_tools()
        except McpConnectionError as exc:
            logger.warning("MCP server %s unreachable: %s", url, exc)
            return []

        async with self._lock:
            # Skip cache write if invalidate() ran during the await
            if key in self._clients:
                self._cache[key] = (time.monotonic(), tools)
        return tools

    async def invoke(
        self, url: str, tool_name: str, args: dict, headers: dict[str, str] | None = None
    ) -> str:
        async with self._lock:
            client = self._get_client(url, headers)
        return await client.invoke(tool_name, args)

    def invalidate(self, url: str) -> None:
        for key in [k for k in self._cache if k[0] == url]:
            self._cache.pop(key, None)
        for key in [k for k in self._clients if k[0] == url]:
            self._clients.pop(key, None)


mcp_pool = McpConnectionPool(
    cache_ttl=settings.mcp_tool_cache_ttl_seconds,
    connect_timeout=settings.mcp_connection_timeout_seconds,
    tool_timeout=settings.mcp_tool_timeout_seconds,
)
