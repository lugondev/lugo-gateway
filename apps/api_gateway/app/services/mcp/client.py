from __future__ import annotations

import httpx


class McpConnectionError(Exception):
    pass


class McpHttpClient:
    """Thin async HTTP adapter for a single MCP HTTP server.

    GET  {url}/tools              → list of tool defs
    POST {url}/tools/{name}       → {"result": str}   body: {"arguments": {}}
    """

    def __init__(
        self,
        url: str,
        connect_timeout: float = 10.0,
        tool_timeout: float = 30.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self._connect_timeout = connect_timeout
        self._tool_timeout = tool_timeout
        self._headers = headers or {}
        # One client for this adapter's whole lifetime, so httpx keeps the
        # TCP+TLS connection alive between calls. Every call used to open its
        # own `async with httpx.AsyncClient(...)`, which meant a full handshake
        # per tool listing AND per tool invocation -- on the conversation's
        # critical path, since a tool call sits between the user's words and the
        # model's reply. Same reasoning as OpenAICompatResponder's client.
        # Per-call timeouts are passed per request instead of per client.
        self._client = httpx.AsyncClient(headers=self._headers)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list_tools(self) -> list[dict]:
        try:
            resp = await self._client.get(
                f"{self.url}/tools", timeout=self._connect_timeout
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else data.get("tools", [])
        except Exception as exc:
            raise McpConnectionError(f"Failed to list tools from {self.url}: {exc}") from exc

    async def invoke(self, tool_name: str, arguments: dict) -> str:
        try:
            resp = await self._client.post(
                f"{self.url}/tools/{tool_name}",
                json={"arguments": arguments},
                timeout=self._tool_timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return str(data.get("result", data))
        except Exception as exc:
            raise McpConnectionError(
                f"Failed to invoke '{tool_name}' on {self.url}: {exc}"
            ) from exc
