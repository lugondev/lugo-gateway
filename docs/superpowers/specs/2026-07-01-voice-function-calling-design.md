# Voice Function-Calling — Design

Date: 2026-07-01
Status: Approved (pending spec review)
Scope: `apps/api_gateway` — add LLM tool/function-calling to the voice conversation
so the assistant can *act* (control an ESP32/IoT device, answer live queries) instead
of only replying with text. Inspired by xiaozhi-server-go's `src/core/mcp/`.

## Goal

Turn the conversation responder from "chat → TTS" into "chat → (optional tool
calls) → TTS". Ship a working local tool registry with two demo tools, and make
the tool source pluggable so an MCP adapter can be added later without touching
the responder.

Non-goals (this iteration): a live MCP client transport; streaming tool-call
accumulation; a tool-permission/auth UI.

## Components

All new code under `apps/api_gateway/app/services/conversation/tools/`.

- **`Tool`** — dataclass/interface: `name: str`, `description: str`,
  `parameters: dict` (JSON Schema), `async run(args: dict, ctx: ToolContext) -> str`.
- **`ToolContext`** — passed into `run`. Carries `emit_command(payload: dict)`
  (an async callback that pushes an event to the connected device over the WS) and
  read-only session info (e.g. language). Decouples tools from the WebSocket.
- **`ToolSource`** — interface with `list_tools() -> list[Tool]`. The pluggable
  seam. Implementations:
  - **`LocalToolSource`** — Python-defined tools. **Fully implemented + tested.**
  - **`McpToolSource`** — adapter that converts an MCP server's tool definitions
    into `Tool` objects. **Interface-ready**: the def→Tool mapping is implemented
    and unit-tested against sample MCP tool JSON, but the live MCP transport
    (connecting to a real server) is a documented integration point, gated until
    validated against a real MCP server. The registry treats it identically.
- **`ToolRegistry`** — aggregates sources. Methods:
  - `openai_schema() -> list[dict]` — tools in OpenAI `tools` request format.
  - `get(name) -> Tool | None`
  - `async run(name, args, ctx) -> str` — dispatch; unknown name → error string.

## Initial tools (LocalToolSource)

- **`get_time`** — no args; returns the current time as a spoken-friendly string.
  Server-side only; proves the LLM→tool→LLM→TTS loop with zero firmware dependency.
- **`device_command`** — args `{action: str, params: object}`; calls
  `ctx.emit_command({"event": "device_command", "action", "params"})` to push a
  command to the device over the WS, and returns a confirmation string to the LLM.
  Firmware-side handling of the event is out of scope (ESP32 follow-up).

## Data flow — two-phase responder

`OpenAICompatResponder.reply_stream(history, ctx=None, registry=None)`:

1. **No registry / tools disabled** → current behaviour: stream tokens → sentences
   → TTS. Default path is unchanged (tools default OFF).
2. **Tools enabled**:
   - **Phase 1 (detect):** `POST /chat/completions` with `stream=false` and
     `tools=registry.openai_schema()`. Loop up to `conversation_tool_max_iters`
     (default 3): if the message has `tool_calls`, run each via `registry.run`,
     append the assistant tool-call message + a `{role: "tool", tool_call_id,
     content}` result, and re-request. Stop when there are no more tool calls.
   - **Phase 2 (speak):** the final message content is segmented and streamed to
     TTS. If phase 1's final response already carried content (no tools used), that
     content is segmented directly — no extra round-trip.

## Error handling

- A tool that raises → its `run` returns an error string; that becomes the tool
  result the model sees, so it can recover / apologise. A turn never crashes on a
  tool error.
- `conversation_tool_max_iters` caps the tool loop to prevent infinite tool cycles.
- An LLM/endpoint that ignores `tools` (e.g. gemma2) simply returns content with no
  `tool_calls` → behaves like a normal reply. **Function-calling only has effect
  with a tools-capable model** (qwen2.5, llama3.1, OpenAI, …). Documented in config.

## Config (settings.py) — default OFF

- `conversation_tools_enabled: bool = False`
- `conversation_tool_max_iters: int = 3`

## Route wiring (conversation.py)

- When `conversation_tools_enabled`, build a `ToolRegistry` with `LocalToolSource`.
- Construct a `ToolContext` whose `emit_command` sends a `device_command` event over
  the existing `send(...)` WS channel.
- Pass `ctx` + `registry` into `responder.reply_stream(...)` in the audio and text
  turn paths. When disabled, pass nothing (unchanged path).

## Trade-offs (accepted)

- With tools enabled, a normal reply loses token-level streaming (phase 1 is
  non-streamed to detect tool calls), so first-audio latency is slightly higher.
  Acceptable because tools are opt-in (default OFF) — current behaviour is
  unchanged. Streaming tool-call accumulation is a future optimisation.

## Testing (TDD)

- **ToolRegistry**: register/list, `openai_schema` shape, `get`, `run` dispatch,
  unknown-tool error. Pure.
- **Local tools**: `get_time` returns a non-empty time string; `device_command`
  invokes `ctx.emit_command` with the right payload and returns a confirmation.
- **McpToolSource**: sample MCP tool-definition JSON → correct `Tool` objects
  (name/description/parameters). Transport not exercised.
- **Responder two-phase**: `httpx.MockTransport` (real responder code, no mocking of
  our logic) serves a canned tool-call response then a final content response;
  assert the tool ran and the final text reached the sentence stream.

## Files

New: `services/conversation/tools/{__init__,base,local,mcp}.py`; tests
`tests/unit/test_conversation_tools.py`, `tests/unit/test_responder_tools.py`.
Changed: `services/conversation/responder.py`, `api/routes/conversation.py`,
`core/settings.py`.
