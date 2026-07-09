"""Device MCP: the gateway is an MCP client to a device that runs an MCP server
over the Lugo WebSocket. This module is transport-owning (the route wires the
websocket send in) and produces a ``ToolSource`` for the LLM.

Envelope: {"type": "mcp", "payload": {<JSON-RPC 2.0>}}. Fixed ids: initialize=1,
tools/list=2; tools/call ids start at 3.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

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
