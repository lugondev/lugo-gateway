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
    ) -> None:
        self.url = url.rstrip("/")
        self._connect_timeout = connect_timeout
        self._tool_timeout = tool_timeout

    async def list_tools(self) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=self._connect_timeout) as client:
                resp = await client.get(f"{self.url}/tools")
                resp.raise_for_status()
                data = resp.json()
                return data if isinstance(data, list) else data.get("tools", [])
        except Exception as exc:
            raise McpConnectionError(f"Failed to list tools from {self.url}: {exc}") from exc

    async def invoke(self, tool_name: str, arguments: dict) -> str:
        try:
            async with httpx.AsyncClient(timeout=self._tool_timeout) as client:
                resp = await client.post(
                    f"{self.url}/tools/{tool_name}",
                    json={"arguments": arguments},
                )
                resp.raise_for_status()
                data = resp.json()
                return str(data.get("result", data))
        except Exception as exc:
            raise McpConnectionError(
                f"Failed to invoke '{tool_name}' on {self.url}: {exc}"
            ) from exc
