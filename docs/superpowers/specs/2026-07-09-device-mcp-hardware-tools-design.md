# Device MCP: hardware tools for the AI agent (ESP32 + gateway)

**Date:** 2026-07-09
**Status:** Design approved, pending implementation plan
**Scope (this spec):** Gateway MCP relay + LLM tool exposure, and the ESP32 `mcp_server` component with a reusable tool-registration template. RPi is a follow-up spec.

## Goal

Let the AI agent (the gateway's LLM) discover and invoke hardware capabilities on a
connected device (read device status, set volume/brightness, show text, drive
GPIO/LED, put the device to idle, shut it down). The device becomes controllable by
voice: "louder", "dim the screen", "go to sleep", "turn yourself off".

The centerpiece is a **reusable template** so adding a new hardware tool is a few
lines with clear docs — including a per-tool confirmation flag for destructive
actions.

## References

Design is grounded in two upstream projects (adapted, not copied; the "xiaozhi" name
must not appear in our code/comments/docs per [[lugo-device-protocol]]):

- **Device firmware:** `78/xiaozhi-esp32` — `main/mcp_server.cc` (`AddTool` +
  `PropertyList`, `initialize`/`tools/list`/`tools/call` handlers, `ReturnValue`
  result serialization).
- **Server:** `xinnan-tech/xiaozhi-esp32-server` —
  `main/xiaozhi-server/core/providers/tools/device_mcp/` (`MCPClient`,
  `mcp_handler.py`: envelope, id-correlated futures, fixed discovery ids, cursor
  pagination, name sanitization, result unwrap, timeout/cleanup).

## Architecture

Roles follow the xiaozhi model:

- **Device (ESP32) = MCP server.** A new `components/mcp_server` C component holds a
  registry of tools. Each tool has: name, description, typed property list, a
  `requires_confirm` flag, and a C callback. It answers JSON-RPC `initialize`,
  `tools/list`, `tools/call`.
- **Gateway = MCP client + relay.** Owns a `DeviceMcpTransport` (request/response
  correlator over the existing Lugo WebSocket) and exposes discovered device tools to
  the LLM as a `DeviceMcpToolSource`, merged into the existing per-session
  `ToolRegistry` alongside local and HTTP-MCP tool sources.

Everything rides the **existing Lugo WebSocket** (`/v1/lugo/stream`). No new
connection. The `mcp` frame type is already reserved on both sides
(`LUGO_EV_MCP` in `lugo_protocol.h`; `emit("command")→{"type":"mcp"}` in
`routes/lugo.py`, to be reconciled — see below).

### Lifecycle

1. Device connects, sends `wakeup` — now including `features: {"mcp": true}`.
2. Gateway replies `welcome` (unchanged).
3. If `features.mcp` is present, gateway runs discovery: `initialize` (id=1) →
   `tools/list` (id=2, looped while `result.nextCursor`). It builds a
   `DeviceMcpToolSource` from the returned defs and adds it to the session's
   `ToolRegistry` — **before the first user turn** (no turn runs until the user
   speaks, so there is always slack).
4. User speaks → the LLM sees device tools alongside local/HTTP-MCP tools. The LLM
   calls e.g. `self_audio_set_volume` → gateway relays `tools/call` over the WS →
   device runs the callback → returns a result → gateway feeds the result back into
   the LLM turn → spoken reply.
5. Destructive tool called without `confirm=true` → gateway short-circuits with a
   synthetic "needs confirmation" result (never reaches the device) until the LLM
   re-calls with `confirm=true`.

```
LLM turn --"self_device_shutdown"--> Gateway --(confirm missing)--> synthetic "confirm?" --> LLM asks user
                                                                                              | user: "yes"
LLM turn --"self_device_shutdown{confirm:true}"--> Gateway --mcp/tools/call--> Device --> shuts down
```

## Wire protocol

`mcp` is a JSON **text** frame carrying a standard **JSON-RPC 2.0** envelope in
`payload` (identical to xiaozhi-server).

**Downlink (gateway → device):**
```json
{ "type": "mcp", "payload": {
    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
    "params": { "name": "self.audio.set_volume", "arguments": { "volume": 70 } } } }
```

**Uplink (device → gateway):**
```json
{ "type": "mcp", "payload": {
    "jsonrpc": "2.0", "id": 1,
    "result": { "content": [ { "type": "text", "text": "volume set to 70" } ],
                "isError": false } } }
```
Errors use the JSON-RPC `error` object (`code`, `message`) and/or `result.isError`.

**`initialize`** params: `protocolVersion "2024-11-05"`, `capabilities`,
`clientInfo {name:"LugoGateway", version}`. (Vision/camera capability from xiaozhi is
out of scope for v1.)

**`tools/list` result** — each tool:
```json
{ "name": "self.audio.set_volume",
  "description": "Set speaker volume (0-100)",
  "inputSchema": { "type": "object",
    "properties": { "volume": { "type": "integer", "minimum": 0, "maximum": 100 } },
    "required": ["volume"] },
  "annotations": { "requiresConfirm": false } }
```
Large tool sets paginate via `result.nextCursor` (gateway re-requests `tools/list`
with `params.cursor`, id=2).

### Correlation IDs

- `initialize` = id 1, `tools/list` = id 2 (reused for cursor continuation).
- `tools/call` = a monotonic counter starting at 3.
- Gateway routes all `mcp` responses through `DeviceMcpTransport.on_message`, which
  resolves the `Future` in `pending[id]`. Unknown/duplicate id → dropped + warned.

## Device side (ESP32 `components/mcp_server`)

New component. The **template is the deliverable**, so it follows the codebase's
existing self-registration idiom (mirrors `LUGO_BOARD_REGISTER` /
[[esp32-board-abstraction]]).

### Registering a tool = one static definition (no central edits)

```c
static mcp_result_t audio_set_volume(const cJSON *args) {
    int v = mcp_arg_int(args, "volume", -1);
    if (v < 0) return mcp_err("missing volume");
    board_active()->audio->set_volume(v);   // through the board vtable
    return mcp_ok_text("volume set to %d", v);
}

LUGO_MCP_TOOL(
    .name = "self.audio.set_volume",
    .description = "Set speaker volume (0-100)",
    .props = (mcp_prop_t[]){ MCP_PROP_INT("volume", 0, 100), MCP_PROP_END },
    .requires_confirm = false,
    .fn = audio_set_volume);
```

`LUGO_MCP_TOOL(...)` places a `mcp_tool_desc_t` into an `mcp_tool` linker section;
`mcp_server_init()` walks the section to build the registry.

**Gotcha (carried from `board_desc`):** `components/mcp_server` CMake needs
**WHOLE_ARCHIVE** and `linker.lf` a `KEEP() SURROUND(mcp_tool)` fragment. Without it,
self-registering objects are dropped before `--gc-sections` and the registry is
silently empty (registered=0).

### Property system (typed, xiaozhi-style)

`MCP_PROP_BOOL(name)`, `MCP_PROP_INT(name, min, max)`, `MCP_PROP_STRING(name)`, with
optional defaults, terminated by `MCP_PROP_END`. Used to (a) validate incoming
`arguments` in `tools/call` and (b) auto-emit `inputSchema` in `tools/list`.

### Result helpers

`mcp_ok_text(fmt, ...)` and `mcp_err(msg)` build the JSON-RPC `content` / error.

### JSON-RPC handler

Wired into `ws_client`'s existing `LUGO_EV_MCP` branch. Handles:
- `initialize` — advertise capabilities + device name/version.
- `tools/list` — walk registry, emit `inputSchema` + `annotations.requiresConfirm`,
  paginate by cursor if it overflows one frame.
- `tools/call` — lookup by name → validate props → run `fn` → serialize result.

Hardware access routes through the **board vtable** (`audio_ops`, `display_ops`), so
tools stay board-agnostic and work on board #2 for free.

### v1 tools (reference set shipped with the template)

| Tool | Confirm | Backed by |
|---|---|---|
| `self.get_device_status` | no | free heap / wifi RSSI / uptime / fw version / current volume+brightness |
| `self.audio.set_volume` | no | `audio_ops` vtable |
| `self.screen.set_brightness` | no | `display_ops` |
| `self.screen.show_text` | no | `display_ops` |
| `self.gpio.set` | **yes** | small GPIO HAL / `gpio_ops` |
| `self.device.idle` | no | FSM → idle (the "go rest" case) |
| `self.device.shutdown` | **yes** | deep sleep / power-off |

`self.gpio.set` and `self.device.shutdown` require small additions (a minimal GPIO
op and a power/sleep op), kept minimal and board-routed.

A `components/mcp_server/README.md` documents "add a tool in 3 lines" and when to set
`requires_confirm`.

## Gateway side

### `DeviceMcpTransport` (owned by the Lugo route)

Mirrors xiaozhi-server's `MCPClient`:
- `async call(method, params, timeout) -> dict`: allocate id (fixed for
  init/list, counter for calls), build JSON-RPC, send via injected `send_json`
  (the route's websocket), store a `Future` in `pending[id]`, await with timeout.
- `on_message(payload)`: resolve/reject `pending[id]`; unknown id → warn + drop.
- `close()`: reject all pending futures (connection gone).

### `DeviceMcpToolSource`

Thin variant of the existing `McpToolSource` (`services/conversation/tools/mcp.py`).
Same `mcp_tool_to_tool` conversion, but:
- Invoker calls `transport.call("tools/call", {"name": real_name, "arguments": a})`
  and unwraps `result.content[0].text` (checks `isError`).
- **Tool-name sanitization:** advertised `self.audio.set_volume` → LLM function name
  `self_audio_set_volume`, with a reverse `name_mapping` restoring the real name on
  call (some LLM providers reject dots). From `sanitize_tool_name`.
- Carries each tool's `annotations.requiresConfirm` for the confirmation gate.

### Confirmation gate (our addition, not in xiaozhi)

Enforced in the invoker wrapper (one place):
```
if requires_confirm and not args.get("confirm"):
    return "CONFIRMATION_REQUIRED: This will <description>. Ask the user to confirm " \
           "out loud, then call again with confirm=true."
# else relay tools/call to the device
```
For confirm-required tools, a `confirm: {"type":"boolean"}` property is injected into
the LLM-facing `inputSchema` so the model knows the parameter exists. The device
never implements confirm logic — it sees only an already-confirmed `tools/call`.

### Wiring into the session

- `routes/lugo.py` creates the transport, runs `initialize` + `tools/list` right
  after `welcome`, builds the `DeviceMcpToolSource`, and hands it to the session.
- `ConversationSession` / `_build_tool_registry` gains an optional
  `extra_tool_sources: list[ToolSource]` param; the device source is appended.
  Browser/text clients pass nothing → behaviour unchanged.
- The route recv loop gains an `mcp` branch:
  `elif ctype == "mcp": transport.on_message(control["payload"])`.
- The legacy `emit("command")→{"type":"mcp"}` one-way path is reconciled so `mcp`
  frames mean JSON-RPC only (rename the legacy path or route it through the same
  channel).

### Config

- `settings.device_mcp_enabled` (default true)
- `settings.device_mcp_request_timeout_s` (~10)
- `settings.device_mcp_discovery_timeout_s` (~10)

## Error handling & edge cases

- Device without `features.mcp` → no discovery, voice unaffected.
- `tools/list` timeout/malformed → 0 device tools, logged, session continues (same as
  an unreachable HTTP MCP server today).
- `tools/call` timeout / connection drop mid-call → future rejected → tool-error
  string to the LLM (turn survives). `transport.close()` rejects all pending on
  disconnect.
- Unknown/duplicate response `id` → dropped + warned.
- Device `fn` returns error → JSON-RPC `error`/`isError` → surfaced to the LLM.
- Barge-in/abort during an in-flight tool call → pending futures rejected on turn
  teardown.
- No retry of discovery in v1 (device tools are static per boot).

## Testing

**Gateway (pytest, host):**
- transport id-correlation + timeout + reject-on-close
- `DeviceMcpToolSource` schema conversion + name sanitization/reverse-map
- confirmation gate (blocks without `confirm`, relays with `confirm=true`)
- discovery sequence including cursor pagination
- registry merge with local/HTTP sources
- graceful no-MCP device (`features.mcp` absent)

Run tests + local endpoint check before pushing to `main` per
[[test-before-push-deploy]] (main auto-deploys to prod).

**ESP32 (host-testable, per firmware pattern):**
- frame / JSON-RPC codec
- property validation
- `tools/list` schema emission
- registry walk

On-target `idf.py flash monitor` is the human verification gate (WHOLE_ARCHIVE
registry can only be confirmed on build/target), same as the board-abstraction work.

## Scope boundaries

**In scope:** gateway relay + LLM exposure + confirmation gate; ESP32 `mcp_server`
component + 7 v1 tools + template docs.

**Out of scope (follow-up specs):**
- RPi Python device MCP server (`scripts/rpi_voice_client.py`)
- vision/camera capability
- on-device wake word
- dynamic tool re-discovery mid-session
