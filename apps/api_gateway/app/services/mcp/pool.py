from __future__ import annotations

import asyncio
import logging
import time

from app.core.settings import settings
from app.services.mcp.client import McpConnectionError, McpHttpClient

logger = logging.getLogger(__name__)


class McpConnectionPool:
    """Lazy-connecting pool of MCP HTTP clients, one per URL.

    Tool definitions are cached per URL with a configurable TTL to avoid
    re-fetching on every session. Call ``invalidate(url)`` when a server's
    config changes to force a fresh fetch.
    """

    def __init__(
        self,
        cache_ttl: float = 300.0,
        connect_timeout: float = 10.0,
        tool_timeout: float = 30.0,
    ) -> None:
        self._clients: dict[str, McpHttpClient] = {}
        self._cache: dict[str, tuple[float, list[dict]]] = {}  # url -> (timestamp, tools)
        self._ttl = cache_ttl
        self._connect_timeout = connect_timeout
        self._tool_timeout = tool_timeout
        self._lock = asyncio.Lock()

    def _get_client(self, url: str) -> McpHttpClient:
        if url not in self._clients:
            self._clients[url] = McpHttpClient(url, self._connect_timeout, self._tool_timeout)
        return self._clients[url]

    async def get_tools(self, url: str) -> list[dict]:
        async with self._lock:
            cached = self._cache.get(url)
            if cached and (time.monotonic() - cached[0]) < self._ttl:
                return cached[1]
            try:
                client = self._get_client(url)
                tools = await client.list_tools()
                # Skip cache write if invalidate() ran during the await
                if url in self._clients:
                    self._cache[url] = (time.monotonic(), tools)
                return tools
            except McpConnectionError as exc:
                logger.warning("MCP server %s unreachable: %s", url, exc)
                return []

    async def invoke(self, url: str, tool_name: str, args: dict) -> str:
        async with self._lock:
            client = self._get_client(url)
        return await client.invoke(tool_name, args)

    def invalidate(self, url: str) -> None:
        self._cache.pop(url, None)
        self._clients.pop(url, None)


mcp_pool = McpConnectionPool(
    cache_ttl=settings.mcp_tool_cache_ttl_seconds,
    connect_timeout=settings.mcp_connection_timeout_seconds,
    tool_timeout=settings.mcp_tool_timeout_seconds,
)
