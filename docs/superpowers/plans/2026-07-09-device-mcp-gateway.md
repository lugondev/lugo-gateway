# Device MCP — Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the gateway's LLM discover and invoke hardware tools that a connected Lugo device (ESP32) advertises as an MCP server, over the existing Lugo WebSocket.

**Architecture:** The gateway acts as an MCP client. A `DeviceMcpTransport` (owned by the Lugo route) sends JSON-RPC requests over the WS and correlates responses to `asyncio.Future`s by id. After `welcome`, a background discovery task runs `initialize` + `tools/list`; the resulting tool defs are wrapped as a `DeviceMcpToolSource` and added to the session's `ToolRegistry` before the first user turn. Destructive tools are gated by a gateway-enforced two-phase confirmation. This is the server side of `docs/superpowers/specs/2026-07-09-device-mcp-hardware-tools-design.md`; the ESP32 firmware is a separate plan.

**Tech Stack:** Python 3.12, FastAPI (WebSocket), asyncio, pytest. No new dependencies.

## Global Constraints

- Python 3.12 (`.venv`); run tests with the repo's pytest.
- Do **not** use the name "xiaozhi" anywhere in code/comments/docs — this is the "Lugo" protocol.
- `mcp` frame envelope is exactly `{"type": "mcp", "payload": {<JSON-RPC 2.0>}}`.
- Fixed discovery ids: `initialize` = 1, `tools/list` = 2 (reused for cursor continuation); `tools/call` ids from a counter starting at 3.
- A tool failure or transport error must **never** crash a turn — it becomes an error string the LLM sees (existing `ToolRegistry.run` contract).
- Run tests + a local endpoint check before pushing to `main` (main auto-deploys to prod).

---

### Task 1: `DeviceMcpTransport` — request/response correlator

**Files:**
- Create: `apps/api_gateway/app/services/conversation/tools/device_mcp.py`
- Test: `tests/unit/test_device_mcp_transport.py`

**Interfaces:**
- Consumes: nothing (leaf module).
- Produces:
  - `class DeviceMcpError(Exception)`
  - `class DeviceMcpTransport:`
    - `__init__(self, send_json: Callable[[dict], Awaitable[None]], request_timeout: float = 10.0)`
    - `async call(self, method: str, params: dict | None = None, *, msg_id: int | None = None, timeout: float | None = None) -> dict` — returns the JSON-RPC `result` object; raises `DeviceMcpError` on JSON-RPC `error`, timeout, or closed transport.
    - `on_message(self, payload: dict) -> None` — resolve/reject the pending future for `payload["id"]`.
    - `close(self) -> None` — reject all pending futures.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_device_mcp_transport.py
import asyncio
import pytest
from app.services.conversation.tools.device_mcp import DeviceMcpTransport, DeviceMcpError


def _collector():
    sent = []
    async def send_json(msg):
        sent.append(msg)
    return sent, send_json


@pytest.mark.asyncio
async def test_call_sends_envelope_and_resolves_on_matching_id():
    sent, send_json = _collector()
    t = DeviceMcpTransport(send_json)

    async def responder():
        await asyncio.sleep(0)  # let call() register the future + send
        mid = sent[-1]["payload"]["id"]
        t.on_message({"jsonrpc": "2.0", "id": mid, "result": {"ok": True}})

    task = asyncio.create_task(responder())
    result = await t.call("tools/call", {"name": "x", "arguments": {}})
    await task

    assert result == {"ok": True}
    env = sent[-1]
    assert env["type"] == "mcp"
    assert env["payload"]["jsonrpc"] == "2.0"
    assert env["payload"]["method"] == "tools/call"
    assert env["payload"]["params"] == {"name": "x", "arguments": {}}
    assert env["payload"]["id"] >= 3  # call ids start at 3


@pytest.mark.asyncio
async def test_fixed_msg_id_is_honored():
    sent, send_json = _collector()
    t = DeviceMcpTransport(send_json)

    async def responder():
        await asyncio.sleep(0)
        t.on_message({"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "dev"}}})

    task = asyncio.create_task(responder())
    result = await t.call("initialize", {"protocolVersion": "2024-11-05"}, msg_id=1)
    await task
    assert result["serverInfo"]["name"] == "dev"
    assert sent[-1]["payload"]["id"] == 1


@pytest.mark.asyncio
async def test_error_payload_raises():
    sent, send_json = _collector()
    t = DeviceMcpTransport(send_json)

    async def responder():
        await asyncio.sleep(0)
        mid = sent[-1]["payload"]["id"]
        t.on_message({"jsonrpc": "2.0", "id": mid, "error": {"code": -1, "message": "boom"}})

    task = asyncio.create_task(responder())
    with pytest.raises(DeviceMcpError, match="boom"):
        await t.call("tools/call", {"name": "x"})
    await task


@pytest.mark.asyncio
async def test_timeout_raises_and_cleans_up():
    _, send_json = _collector()
    t = DeviceMcpTransport(send_json, request_timeout=0.01)
    with pytest.raises(DeviceMcpError, match="timed out"):
        await t.call("tools/call", {"name": "x"})
    assert t._pending == {}


@pytest.mark.asyncio
async def test_unknown_id_is_dropped_not_raised():
    _, send_json = _collector()
    t = DeviceMcpTransport(send_json)
    t.on_message({"jsonrpc": "2.0", "id": 999, "result": {}})  # must not raise


@pytest.mark.asyncio
async def test_close_rejects_pending():
    sent, send_json = _collector()
    t = DeviceMcpTransport(send_json)
    fut_task = asyncio.create_task(t.call("tools/call", {"name": "x"}))
    await asyncio.sleep(0)  # register future
    t.close()
    with pytest.raises(DeviceMcpError, match="closed"):
        await fut_task
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_device_mcp_transport.py -v`
Expected: FAIL — `ModuleNotFoundError: ... device_mcp`

- [ ] **Step 3: Write the implementation**

```python
# apps/api_gateway/app/services/conversation/tools/device_mcp.py
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
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[mid] = fut
        payload: dict = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            await self._send({"type": "mcp", "payload": payload})
            return await asyncio.wait_for(fut, timeout or self._timeout)
        except asyncio.TimeoutError as exc:
            raise DeviceMcpError(f"{method} timed out") from exc
        finally:
            self._pending.pop(mid, None)

    def on_message(self, payload: dict) -> None:
        mid = payload.get("id")
        fut = self._pending.get(mid) if isinstance(mid, int) else None
        if fut is None or fut.done():
            logger.warning("device mcp: dropping response for unknown/stale id %r", mid)
            return
        if "error" in payload:
            fut.set_exception(DeviceMcpError(str(payload["error"])))
        else:
            fut.set_result(payload.get("result", {}))

    def close(self) -> None:
        self._closed = True
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(DeviceMcpError("transport closed"))
        self._pending.clear()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_device_mcp_transport.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/conversation/tools/device_mcp.py tests/unit/test_device_mcp_transport.py
git commit -m "feat(gateway): DeviceMcpTransport request/response correlator"
```

---

### Task 2: `DeviceMcpToolSource` — schema conversion, name sanitization, confirm gate

**Files:**
- Modify: `apps/api_gateway/app/services/conversation/tools/device_mcp.py`
- Test: `tests/unit/test_device_mcp_tool_source.py`

**Interfaces:**
- Consumes: `DeviceMcpTransport` (Task 1), `Tool`/`ToolSource`/`ToolContext` from `app.services.conversation.tools.base`.
- Produces:
  - `def sanitize_tool_name(name: str) -> str` — replace every char outside `[A-Za-z0-9_-]` with `_`.
  - `class DeviceMcpToolSource(ToolSource):`
    - `__init__(self, tool_defs: list[dict], transport: DeviceMcpTransport)`
    - `list_tools(self) -> list[Tool]`
  - Each produced `Tool`: name is sanitized; if the def's `annotations.requiresConfirm` is truthy a `confirm` boolean is injected into `parameters.properties`; its `run` enforces confirmation, strips `confirm` from arguments, calls `transport.call("tools/call", {"name": <real>, "arguments": <args>})`, and unwraps `result.content[0].text` (honoring `result.isError`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_device_mcp_tool_source.py
import pytest
from app.services.conversation.tools.base import ToolContext
from app.services.conversation.tools.device_mcp import (
    DeviceMcpToolSource, sanitize_tool_name,
)


class _FakeTransport:
    def __init__(self, result):
        self._result = result
        self.calls = []
    async def call(self, method, params=None, **kw):
        self.calls.append((method, params))
        return self._result


def test_sanitize_replaces_dots():
    assert sanitize_tool_name("self.audio.set_volume") == "self_audio_set_volume"
    assert sanitize_tool_name("ok-name_1") == "ok-name_1"


def test_list_tools_sanitizes_name_and_maps_schema():
    defs = [{
        "name": "self.audio.set_volume",
        "description": "Set speaker volume",
        "inputSchema": {"type": "object",
                        "properties": {"volume": {"type": "integer"}},
                        "required": ["volume"]},
        "annotations": {"requiresConfirm": False},
    }]
    src = DeviceMcpToolSource(defs, _FakeTransport({}))
    tools = src.list_tools()
    assert tools[0].name == "self_audio_set_volume"
    assert "volume" in tools[0].parameters["properties"]
    assert "confirm" not in tools[0].parameters["properties"]


def test_confirm_tool_injects_confirm_property():
    defs = [{"name": "self.device.shutdown", "description": "Power off",
             "inputSchema": {"type": "object", "properties": {}},
             "annotations": {"requiresConfirm": True}}]
    src = DeviceMcpToolSource(defs, _FakeTransport({}))
    tool = src.list_tools()[0]
    assert tool.parameters["properties"]["confirm"]["type"] == "boolean"


@pytest.mark.asyncio
async def test_confirm_required_without_confirm_blocks_and_does_not_call():
    ft = _FakeTransport({"content": [{"type": "text", "text": "off"}]})
    defs = [{"name": "self.device.shutdown", "description": "Power off",
             "annotations": {"requiresConfirm": True}}]
    tool = DeviceMcpToolSource(defs, ft).list_tools()[0]
    out = await tool.run({}, ToolContext())
    assert "CONFIRMATION_REQUIRED" in out
    assert ft.calls == []


@pytest.mark.asyncio
async def test_confirm_true_relays_with_real_name_and_strips_confirm():
    ft = _FakeTransport({"content": [{"type": "text", "text": "powered off"}]})
    defs = [{"name": "self.device.shutdown", "description": "Power off",
             "annotations": {"requiresConfirm": True}}]
    tool = DeviceMcpToolSource(defs, ft).list_tools()[0]
    out = await tool.run({"confirm": True}, ToolContext())
    assert out == "powered off"
    method, params = ft.calls[0]
    assert method == "tools/call"
    assert params == {"name": "self.device.shutdown", "arguments": {}}


@pytest.mark.asyncio
async def test_non_confirm_tool_relays_and_unwraps_text():
    ft = _FakeTransport({"content": [{"type": "text", "text": "volume set to 70"}]})
    defs = [{"name": "self.audio.set_volume", "description": "vol",
             "annotations": {"requiresConfirm": False}}]
    tool = DeviceMcpToolSource(defs, ft).list_tools()[0]
    out = await tool.run({"volume": 70}, ToolContext())
    assert out == "volume set to 70"
    assert ft.calls[0][1] == {"name": "self.audio.set_volume", "arguments": {"volume": 70}}


@pytest.mark.asyncio
async def test_is_error_result_becomes_error_string():
    ft = _FakeTransport({"isError": True, "error": "bad pin"})
    defs = [{"name": "self.gpio.set", "description": "gpio"}]
    tool = DeviceMcpToolSource(defs, ft).list_tools()[0]
    out = await tool.run({"pin": 2}, ToolContext())
    assert "Error" in out and "bad pin" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_device_mcp_tool_source.py -v`
Expected: FAIL — `ImportError: cannot import name 'DeviceMcpToolSource'`

- [ ] **Step 3: Add the implementation to `device_mcp.py`**

Append to `apps/api_gateway/app/services/conversation/tools/device_mcp.py`:

```python
import re

from .base import Tool, ToolContext, ToolSource


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
            result = await transport.call(
                "tools/call", {"name": real_name, "arguments": call_args}
            )
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_device_mcp_tool_source.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/conversation/tools/device_mcp.py tests/unit/test_device_mcp_tool_source.py
git commit -m "feat(gateway): DeviceMcpToolSource with name sanitization + confirm gate"
```

---

### Task 3: `discover_device_tools` — initialize + paginated tools/list

**Files:**
- Modify: `apps/api_gateway/app/services/conversation/tools/device_mcp.py`
- Test: `tests/unit/test_device_mcp_discovery.py`

**Interfaces:**
- Consumes: `DeviceMcpTransport` (Task 1).
- Produces:
  - `async def discover_device_tools(transport: DeviceMcpTransport, *, discovery_timeout: float = 10.0) -> list[dict]` — sends `initialize` (id=1) then `tools/list` (id=2) looping while the result has a truthy `nextCursor`; returns the accumulated list of tool def dicts. On any `DeviceMcpError` returns `[]` (logged) — never raises.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_device_mcp_discovery.py
import asyncio
import pytest
from app.services.conversation.tools.device_mcp import (
    DeviceMcpTransport, discover_device_tools, DeviceMcpError,
)


class _ScriptedDevice:
    """Answers initialize + one or more tools/list pages via on_message."""
    def __init__(self, transport, pages):
        self.t = transport
        self.pages = pages  # list of result dicts for successive tools/list calls
    async def pump(self, sent):
        # Respond to whatever the transport just sent.
        while True:
            await asyncio.sleep(0)
            if not sent:
                continue
            payload = sent[-1]["payload"]
            mid = payload["id"]
            if payload["method"] == "initialize":
                self.t.on_message({"jsonrpc": "2.0", "id": mid,
                                   "result": {"serverInfo": {"name": "dev"}}})
            elif payload["method"] == "tools/list":
                page = self.pages.pop(0)
                self.t.on_message({"jsonrpc": "2.0", "id": mid, "result": page})
                if not page.get("nextCursor"):
                    return


@pytest.mark.asyncio
async def test_discovery_single_page():
    sent = []
    async def send_json(m): sent.append(m)
    t = DeviceMcpTransport(send_json)
    dev = _ScriptedDevice(t, [{"tools": [{"name": "self.audio.set_volume"}]}])
    pump = asyncio.create_task(dev.pump(sent))
    tools = await discover_device_tools(t)
    pump.cancel()
    assert [x["name"] for x in tools] == ["self.audio.set_volume"]
    # initialize used id 1, tools/list used id 2
    ids = [m["payload"]["id"] for m in sent]
    assert ids[0] == 1 and ids[1] == 2


@pytest.mark.asyncio
async def test_discovery_follows_cursor():
    sent = []
    async def send_json(m): sent.append(m)
    t = DeviceMcpTransport(send_json)
    dev = _ScriptedDevice(t, [
        {"tools": [{"name": "a"}], "nextCursor": "c1"},
        {"tools": [{"name": "b"}]},
    ])
    pump = asyncio.create_task(dev.pump(sent))
    tools = await discover_device_tools(t)
    pump.cancel()
    assert [x["name"] for x in tools] == ["a", "b"]
    # second tools/list carried the cursor
    list_calls = [m["payload"] for m in sent if m["payload"]["method"] == "tools/list"]
    assert list_calls[1]["params"] == {"cursor": "c1"}


@pytest.mark.asyncio
async def test_discovery_returns_empty_on_timeout():
    async def send_json(m): pass
    t = DeviceMcpTransport(send_json, request_timeout=0.01)
    tools = await discover_device_tools(t, discovery_timeout=0.01)
    assert tools == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_device_mcp_discovery.py -v`
Expected: FAIL — `ImportError: cannot import name 'discover_device_tools'`

- [ ] **Step 3: Add the implementation to `device_mcp.py`**

```python
_INIT_PARAMS = {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": {"name": "LugoGateway", "version": "1.0.0"},
}


async def discover_device_tools(
    transport: DeviceMcpTransport, *, discovery_timeout: float = 10.0
) -> list[dict]:
    """Run initialize + paginated tools/list. Returns [] on any error."""
    try:
        await transport.call("initialize", _INIT_PARAMS, msg_id=1, timeout=discovery_timeout)
        tools: list[dict] = []
        cursor: str | None = None
        while True:
            params = {"cursor": cursor} if cursor else None
            result = await transport.call(
                "tools/list", params, msg_id=2, timeout=discovery_timeout
            )
            page = result.get("tools") if isinstance(result, dict) else None
            if isinstance(page, list):
                tools.extend(t for t in page if isinstance(t, dict) and t.get("name"))
            cursor = result.get("nextCursor") if isinstance(result, dict) else None
            if not cursor:
                return tools
    except DeviceMcpError as exc:
        logger.warning("device mcp discovery failed: %s", exc)
        return []
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_device_mcp_discovery.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/conversation/tools/device_mcp.py tests/unit/test_device_mcp_discovery.py
git commit -m "feat(gateway): device MCP discovery (initialize + paginated tools/list)"
```

---

### Task 4: `ConversationSession.add_tool_source`

**Files:**
- Modify: `apps/api_gateway/app/services/conversation/session.py` (add method near the `tool_registry` handling; `_build_tool_registry` at :59 is unchanged)
- Test: `tests/unit/test_session_add_tool_source.py`

**Interfaces:**
- Consumes: `ToolRegistry`, `ToolSource` (already imported in `session.py`).
- Produces: `ConversationSession.add_tool_source(self, source: ToolSource) -> None` — appends a source to `self.tool_registry`, creating the registry if it is currently `None`. Because `responder` reads `self.tool_registry` at turn time, a source added before the first turn is visible to the LLM.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_session_add_tool_source.py
from app.services.conversation.session import ConversationSession, SessionRuntimeConfig
from app.services.conversation.tools.base import Tool, ToolSource, ToolContext


class _OneToolSource(ToolSource):
    def list_tools(self):
        async def run(args, ctx): return "ok"
        return [Tool(name="t1", description="", parameters={"type": "object"}, run=run)]


def _cfg():
    return SessionRuntimeConfig(
        session_id="s", profile_name=None, stt_engine="x", language=None,
        tts_engine="x", voice=None, ref_audio_path=None, ref_text=None,
        tts_instruct=None, tts_speed=None, tts_language=None, sample_rate=16000,
        output_sample_rate=24000, audio_codec="pcm16", want_audio=False,
        want_text=True, audio_out="url", denoise=False, resume_sid=None,
    )


async def _noop_emit(*a, **k): ...


def test_add_tool_source_creates_registry_when_none():
    s = ConversationSession(_cfg(), _noop_emit, _noop_emit)
    assert s.tool_registry is None
    s.add_tool_source(_OneToolSource())
    assert s.tool_registry is not None
    assert s.tool_registry.get("t1") is not None


def test_add_tool_source_appends_to_existing_registry():
    from app.services.conversation.tools.base import ToolRegistry
    s = ConversationSession(_cfg(), _noop_emit, _noop_emit)
    s.tool_registry = ToolRegistry([])
    s.add_tool_source(_OneToolSource())
    assert s.tool_registry.get("t1") is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_session_add_tool_source.py -v`
Expected: FAIL — `AttributeError: 'ConversationSession' object has no attribute 'add_tool_source'`

- [ ] **Step 3: Add the method to `ConversationSession`**

Insert right after `is_turn_active` (`session.py:141-142`):

```python
    def add_tool_source(self, source) -> None:
        """Add a ToolSource after start(); used to register device MCP tools
        discovered over the WS. Creates the registry if none exists yet."""
        if self.tool_registry is None:
            self.tool_registry = ToolRegistry([source])
        else:
            self.tool_registry.add_source(source)
```

Ensure `ToolRegistry` is imported (it is, at `session.py:33`).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_session_add_tool_source.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/conversation/session.py tests/unit/test_session_add_tool_source.py
git commit -m "feat(gateway): ConversationSession.add_tool_source for post-start tool registration"
```

---

### Task 5: Wire device MCP into the Lugo route + settings

**Files:**
- Modify: `apps/api_gateway/app/core/settings.py` (near the mcp settings at :219-226)
- Modify: `apps/api_gateway/app/api/routes/lugo.py`
- Test: `tests/unit/test_lugo_device_mcp.py`

**Interfaces:**
- Consumes: `DeviceMcpTransport`, `DeviceMcpToolSource`, `discover_device_tools` (Tasks 1–3); `ConversationSession.add_tool_source` (Task 4).
- Produces: an end-to-end path — a device that sends `wakeup` with `features: {"mcp": true}`, answers `initialize`/`tools/list`, then has its tools invoked via a text turn; plus settings `device_mcp_enabled`, `device_mcp_request_timeout_s`, `device_mcp_discovery_timeout_s`.

- [ ] **Step 1: Add settings**

In `apps/api_gateway/app/core/settings.py`, after line 226 (`conversation_tools_enabled`):

```python
    device_mcp_enabled: bool = True
    device_mcp_request_timeout_s: float = 10.0
    device_mcp_discovery_timeout_s: float = 10.0
```

- [ ] **Step 2: Write the failing integration test**

This test uses an LLM stub that always calls the device tool, so no real LLM is needed. It monkeypatches the responder builder to a stub that emits one tool call then a final text.

```python
# tests/unit/test_lugo_device_mcp.py
import json
import pytest
from fastapi.testclient import TestClient
from app.core.settings import settings
from app.main import app
from app.schemas.stt import STTResult
from app.services.profiles.models import Profile, SessionConfig
from app.services.profiles.store import ProfileStore
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service


class _StubSTT(STTProvider):
    name = "stub-mcp-stt"
    async def transcribe_bytes(self, audio_bytes, language=None):
        return STTResult(engine=self.name, text="louder please", is_final=True)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "conversation_llm_base_url", "")
    monkeypatch.setattr(settings, "conversation_stt_engine", "stub-mcp-stt")
    monkeypatch.setattr(settings, "device_mcp_enabled", True)
    stt_service.providers["stub-mcp-stt"] = _StubSTT()
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    fresh.upsert(Profile(name="dev", session=SessionConfig(idle_timeout_s=0)))
    monkeypatch.setattr("app.api.routes.lugo.profile_store", fresh)
    yield
    stt_service.providers.pop("stub-mcp-stt", None)


def _device_answer(payload: dict) -> dict | None:
    """Given a downlink mcp payload, return the uplink result payload."""
    mid = payload["id"]
    method = payload["method"]
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {"serverInfo": {"name": "dev"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [{
            "name": "self.audio.set_volume",
            "description": "Set speaker volume",
            "inputSchema": {"type": "object",
                            "properties": {"volume": {"type": "integer"}},
                            "required": ["volume"]},
            "annotations": {"requiresConfirm": False},
        }]}}
    if method == "tools/call":
        return {"jsonrpc": "2.0", "id": mid,
                "result": {"content": [{"type": "text", "text": "volume set to 90"}]}}
    return None


def test_device_tools_are_discovered_and_callable():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": "dev",
                      "features": {"mcp": True},
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        assert ws.receive_json()["type"] == "welcome"

        # The gateway drives discovery: answer initialize then tools/list.
        seen_methods = []
        got_tools_call = False
        # Answer downlink mcp frames until we've serviced a tools/call.
        for _ in range(20):
            msg = ws.receive_json()
            if msg.get("type") != "mcp":
                continue
            payload = msg["payload"]
            seen_methods.append(payload["method"])
            ans = _device_answer(payload)
            if ans is not None:
                ws.send_json({"type": "mcp", "payload": ans})
            if payload["method"] == "tools/list":
                # Discovery done; now trigger a turn that calls the tool.
                ws.send_json({"type": "text", "text": "louder"})
            if payload["method"] == "tools/call":
                got_tools_call = True
                assert payload["params"]["name"] == "self.audio.set_volume"
                break
        assert "initialize" in seen_methods
        assert "tools/list" in seen_methods
        assert got_tools_call
```

> **Note for the implementer:** this test depends on the LLM stub in Step 3 that always calls `self_audio_set_volume`. With `conversation_llm_base_url=""` the responder must be stubbed. Patch `app.services.conversation.session.build_responder_ex` (or the symbol `session.py` calls) to return a fake responder whose turn yields one tool call to `self_audio_set_volume` then a final text. Match the existing responder interface used at `session.py:187` and `responder.py`. If the responder seam differs, adapt the patch target — the assertions (discovery methods seen + a `tools/call` for the real tool name) are what matters.

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_lugo_device_mcp.py -v`
Expected: FAIL — no `mcp` frames are sent (route doesn't do discovery yet).

- [ ] **Step 4: Implement the route wiring**

In `apps/api_gateway/app/api/routes/lugo.py`:

(a) Add imports near the top:

```python
from app.services.conversation.tools.device_mcp import (
    DeviceMcpToolSource, DeviceMcpTransport, discover_device_tools,
)
```

(b) After `welcome` is sent (`routes/lugo.py:143-146`) and before `closing = False`, set up the transport + discovery. The device advertises MCP in `wakeup` via `features.mcp`:

```python
    device_mcp = bool((hello.get("features") or {}).get("mcp"))
    transport: DeviceMcpTransport | None = None
    discovery_task: asyncio.Task | None = None
    if device_mcp and settings.device_mcp_enabled:
        transport = DeviceMcpTransport(
            websocket.send_json,
            request_timeout=settings.device_mcp_request_timeout_s,
        )

        async def _discover() -> None:
            defs = await discover_device_tools(
                transport, discovery_timeout=settings.device_mcp_discovery_timeout_s
            )
            if defs:
                session.add_tool_source(DeviceMcpToolSource(defs, transport))
                logger.info("device mcp: registered %d tool(s)", len(defs))

        discovery_task = asyncio.create_task(_discover())
```

(c) In the recv loop, add an `mcp` branch alongside `text`/`abort`/`listen` (after `routes/lugo.py:217`):

```python
                elif ctype == "mcp":
                    if transport is not None:
                        transport.on_message(control.get("payload") or {})
```

(d) In the `finally` block (`routes/lugo.py:220-229`), before `await session.close()`, tear down MCP:

```python
        if discovery_task is not None:
            discovery_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await discovery_task
        if transport is not None:
            transport.close()
```

(e) Reconcile the legacy command path. The existing `emit` handler maps
`event == "command"` to `{"type": "mcp", ...}` (`routes/lugo.py:132-133`). That
one-way "command" frame now collides with real JSON-RPC `mcp` frames. Change it to
a distinct frame so `mcp` means JSON-RPC only:

```python
        elif event == "command":
            await websocket.send_json({"type": "command", **payload})
```

- [ ] **Step 5: Run the integration test**

Run: `.venv/bin/pytest tests/unit/test_lugo_device_mcp.py -v`
Expected: PASS

- [ ] **Step 6: Run the full Lugo + MCP + tools suites (no regressions)**

Run: `.venv/bin/pytest tests/unit/test_lugo_stream.py tests/unit/test_lugo_barge_in.py tests/unit/test_lugo_idle_timeout.py tests/unit/test_conversation_tools.py tests/unit/test_device_mcp_transport.py tests/unit/test_device_mcp_tool_source.py tests/unit/test_device_mcp_discovery.py tests/unit/test_session_add_tool_source.py -v`
Expected: all PASS. (If any pre-existing failure appears in `test_conversation_engine_ready.py`, that is unrelated — see [[lugo-device-protocol]].)

- [ ] **Step 7: Commit**

```bash
git add apps/api_gateway/app/api/routes/lugo.py apps/api_gateway/app/core/settings.py tests/unit/test_lugo_device_mcp.py
git commit -m "feat(gateway): wire device MCP discovery + relay into Lugo route"
```

---

## Self-Review

**Spec coverage:**
- Architecture / roles (gateway = MCP client) → Tasks 1–5. ✓
- Wire protocol envelope + fixed ids + pagination → Tasks 1, 3. ✓
- Handshake gate (`features.mcp`) → Task 5(b). ✓
- Discovery sequence before first turn (background task + `add_tool_source`) → Tasks 4, 5. ✓
- Name sanitization + reverse map (real name on wire) → Task 2. ✓
- Confirmation gate (`requiresConfirm` + injected `confirm` arg) → Task 2. ✓
- Result unwrap (`isError`, `content[0].text`) → Task 2. ✓
- Registry merge with local/HTTP sources → Task 4 (`add_source`) + existing `_build_tool_registry`. ✓
- Error handling (timeout → tool-error; close rejects pending; unknown id dropped; discovery failure → 0 tools) → Tasks 1, 3, 5. ✓
- Legacy `emit("command")` reconciliation → Task 5(e). ✓
- Config settings → Task 5(1). ✓
- Vision/RPi/wake-word/re-discovery → out of scope per spec. ✓ (not planned)

**Placeholder scan:** No TBD/TODO. The one soft spot is the responder stub in Task 5 Step 2 — flagged explicitly with the interface to match and fallback assertions, because the exact `build_responder_ex` seam must be read at implementation time rather than guessed.

**Type consistency:** `DeviceMcpTransport.call(method, params=None, *, msg_id=None, timeout=None) -> dict` used consistently across Tasks 1/2/3/5. `discover_device_tools(transport, *, discovery_timeout)` matches Task 3 def and Task 5 call. `add_tool_source(source)` matches Task 4 def and Task 5 call. `DeviceMcpToolSource(tool_defs, transport)` matches Task 2 def and Task 5 construction.
