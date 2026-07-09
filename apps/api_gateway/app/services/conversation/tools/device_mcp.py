"""Device MCP: the gateway is an MCP client to a device that runs an MCP server
over the Lugo WebSocket. This module is transport-owning (the route wires the
websocket send in) and produces a ``ToolSource`` for the LLM.

Envelope: {"type": "mcp", "payload": {<JSON-RPC 2.0>}}. Fixed ids: initialize=1,
tools/list=2; tools/call ids start at 3.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable

from .base import Tool, ToolContext, ToolSource

logger = logging.getLogger(__name__)


class DeviceMcpError(Exception):
    pass


class DeviceMcpTransport:
    def __init__(
        self,
        send_json: Callable[[dict], Awaitable[None]],
        request_timeout: float = 10.0,
    ) -> None:
        self._send = send_json
        self._timeout = request_timeout
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 3  # 1=initialize, 2=tools/list are reserved
        self._closed = False

    def _alloc_id(self) -> int:
        mid = self._next_id
        self._next_id += 1
        return mid

    async def call(
        self,
        method: str,
        params: dict | None = None,
        *,
        msg_id: int | None = None,
        timeout: float | None = None,
    ) -> dict:
        if self._closed:
            raise DeviceMcpError("transport closed")
        mid = msg_id if msg_id is not None else self._alloc_id()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        payload: dict = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            await self._send({"type": "mcp", "payload": payload})
            return await asyncio.wait_for(fut, timeout or self._timeout)
        except asyncio.TimeoutError as exc:
            raise DeviceMcpError(f"{method} timed out") from exc
        except DeviceMcpError:
            raise
        except Exception as exc:
            raise DeviceMcpError(str(exc)) from exc
        finally:
            self._pending.pop(mid, None)

    def on_message(self, payload: dict) -> None:
        mid = payload.get("id")
        fut = self._pending.get(mid) if isinstance(mid, int) else None
        if fut is None or fut.done():
            logger.warning("device mcp: dropping response for unknown/stale id %r", mid)
            return
        if "error" in payload:
            err = payload["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            fut.set_exception(DeviceMcpError(msg))
        else:
            fut.set_result(payload.get("result", {}))

    def close(self) -> None:
        self._closed = True
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(DeviceMcpError("transport closed"))
        self._pending.clear()


def sanitize_tool_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


class DeviceMcpToolSource(ToolSource):
    """A ToolSource backed by device-advertised MCP tool defs and a transport.

    Names are sanitized for the LLM (dots -> underscores); the real name is
    used on the wire. Tools whose ``annotations.requiresConfirm`` is truthy get
    a ``confirm`` boolean injected into their schema and are gated: the LLM must
    call again with ``confirm=true`` before the call reaches the device.
    """

    def __init__(self, tool_defs: list[dict], transport: DeviceMcpTransport) -> None:
        self._defs = tool_defs
        self._transport = transport

    def list_tools(self) -> list[Tool]:
        tools: list[Tool] = []
        for d in self._defs:
            real = d["name"]
            description = d.get("description", "")
            requires_confirm = bool((d.get("annotations") or {}).get("requiresConfirm"))
            params = dict(d.get("inputSchema") or {"type": "object"})
            if requires_confirm:
                props = dict(params.get("properties") or {})
                props["confirm"] = {
                    "type": "boolean",
                    "description": "Must be true to execute this action. Ask the user to confirm first.",
                }
                params = {**params, "properties": props}
            tools.append(
                Tool(
                    name=sanitize_tool_name(real),
                    description=description,
                    parameters=params,
                    run=self._make_run(real, description, requires_confirm),
                )
            )
        return tools

    def _make_run(self, real_name: str, description: str, requires_confirm: bool):
        transport = self._transport

        async def run(args: dict, ctx: ToolContext) -> str:
            args = args or {}
            if requires_confirm and not args.get("confirm"):
                return (
                    f"CONFIRMATION_REQUIRED: This will {description or real_name}. "
                    "Ask the user to confirm out loud, then call again with confirm=true."
                )
            call_args = {k: v for k, v in args.items() if k != "confirm"}
            try:
                result = await transport.call(
                    "tools/call", {"name": real_name, "arguments": call_args}
                )
            except DeviceMcpError as exc:
                return f"Error: {exc}"
            if isinstance(result, dict) and result.get("isError"):
                return f"Error: {result.get('error') or result}"
            content = result.get("content") if isinstance(result, dict) else None
            if (
                isinstance(content, list)
                and content
                and isinstance(content[0], dict)
                and "text" in content[0]
            ):
                return content[0]["text"]
            return str(result)

        return run
