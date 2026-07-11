# RPi agent-assistant: MCP device tools via lugo protocol

Date: 2026-07-12
Repos touched: `speech-text-transformer` (gateway, `apps/api_gateway`), `agent-assistant` (RPi client)
Repo NOT touched: `esp32-assistant` (changes are additive/backward-compatible for it)

## Context

`esp32-assistant` (ESP32 firmware) and `agent-assistant` (Raspberry Pi client) are both thin
voice clients for the same gateway. A feature comparison found esp32-assistant has an
on-device MCP tool-calling surface (7 tools an LLM can invoke: device status, volume,
idle, shutdown, screen text/backlight, raw GPIO) that agent-assistant has none of. This is
the first of several planned cross-pollination items between the two clients; this spec
covers only this one: **bringing MCP tool-calling to the RPi client**.

agent-assistant currently talks to the gateway over `/v1/conversation/stream` (plain JSON
control messages + raw Opus binary frames, query-param configured). esp32-assistant talks
to a different endpoint, `/v1/lugo/stream`, using a custom binary+JSON wire protocol
(`lugo_protocol`) that already has a working, transport-agnostic MCP tool-calling mechanism
server-side (`app/services/conversation/tools/device_mcp.py`). `/v1/conversation/stream`
has zero MCP wiring today.

Two ways to get MCP onto the RPi client were considered: (a) wire `device_mcp` into
`conversation.py` (the RPi's current endpoint), or (b) switch the RPi client onto
`/v1/lugo/stream` to reuse the existing wiring as-is. **(b) was chosen.** This trades a
larger one-time client rewrite (new wire protocol) for zero new gateway MCP code — but it
initially appeared to drop two features the RPi client already has (session resume,
cold-start "warming up" UX), because `lugo.py`'s wire adapter currently discards the
underlying `ConversationSession` events those features depend on. Investigation confirmed
`ConversationSession` (the protocol-neutral core shared by both routes) already produces
everything needed (`stt_ready`/`tts_ready`/`engines_ready`, resumable session history); only
`lugo.py`'s thin wire-translation layer needs to stop discarding it. That keeps this a
`lugo.py`-only gateway change with no core session changes and no risk to `conversation.py`
or any other consumer of the core.

## Goals

- RPi client can be controlled by the LLM via MCP tools: check status, set volume, go
  idle, show custom text on the OLED.
- RPi client keeps its existing session-resume and cold-start-warming UX after switching
  wire protocols (no regression).
- ESP32 firmware is unaffected — all gateway changes are new/optional wire events it
  already safely ignores (`LUGO_EV_UNKNOWN` → no-op), confirmed in `main.c:516`.

## Non-goals (deferred to future sub-projects)

- Physical buttons, WiFi provisioning, board abstraction, battery monitoring on the RPi
  side (do not port from ESP32 in this pass — no clear hardware need on a mains-powered,
  OS-networked Pi without confirmed peripherals).
- Voice-activated barge-in, STT pre-warm UX, and session resume being ported *from* RPi
  *to* ESP32 (separate future sub-project; this spec only makes sure RPi doesn't lose what
  it already has while gaining MCP).
- Full parity with ESP32's 7 MCP tools. Only 4 are implemented (see below); `shutdown` and
  raw `gpio.set` are excluded — no safe, well-defined semantics on a general Linux host
  without a per-deployment pin/command allowlist, which is out of scope here.
- Any changes to `esp32-assistant`.

## Part 1 — Gateway changes (`apps/api_gateway/app/api/routes/lugo.py`)

`ConversationSession.start()` already computes and emits `stt_ready`/`tts_ready` (via the
`session_started` core event) and later an `engines_ready` core event once both engines
finish loading. `lugo.py`'s `emit()` callback currently drops `session_started`,
`engines_ready`, `speech_start`, `speech_end`, and `processing` entirely (comment: "not on
the wire"), and hardcodes `resume_sid=None` in the `SessionRuntimeConfig` it builds. Four
additive changes, no core changes:

1. **Resume**: accept an optional `"session_id"` field in the client's `wakeup` JSON. If
   present, use it as the session id and pass it through as `resume_sid` (mirroring exactly
   what `conversation.py` already does with its `?session_id=` query param) instead of the
   current `resume_sid=None`.
2. **Engines-ready gating info**: capture `stt_ready`/`tts_ready` from `session.start()`
   and include them in the `welcome` message already sent right after
   (`{"type":"welcome", ..., "stt_ready": bool, "tts_ready": bool}`).
3. **Forward `engines_ready`**: `elif event == "engines_ready": await websocket.send_json({"type": "engines_ready"})`.
4. **Forward turn-detail events for UI parity**: also forward `speech_start`, `speech_end`,
   `processing`, and the `reason` field of `aborted` onto the wire (new JSON message types,
   e.g. `{"type":"speech_start"}` / `{"type":"speech_end"}` / `{"type":"processing"}`, and
   include `reason` on the existing `aborted`-driven `tts{state:"stop"}` translation). These
   are not required for MCP itself, but without them the RPi client loses the
   listening/processing UI distinction it has today (see Part 2). Confirmed safe for
   ESP32: any unrecognized `type` hits `default: break; // LUGO_EV_UNKNOWN` in
   `main.c:516`.

`features.mcp` opt-in and the `DeviceMcpTransport`/`discover_device_tools`/
`DeviceMcpToolSource` wiring already exist in `lugo.py` and need no changes — the RPi
client just needs to send `"features": {"mcp": true}` in its `wakeup`, same as any lugo
device.

## Part 2 — RPi client architecture (`agent-assistant/a2a_client/`)

### New file: `lugo_frame.py` (~15 lines)

Port of the gateway's `lugo_frame.py`: `encode_frame`/`decode_frame` for the v3 binary
header (`struct(">BBH")`: type, reserved, big-endian payload size). Used **only for
downlink** — the server always wraps reply Opus packets in this header via its own
`emit_audio`. **Uplink is unchanged**: `lugo.py` accepts unwrapped raw Opus binary frames
on the way in (confirmed: `feed_audio(message["bytes"])` called directly, comment "v3
wrapping optional on uplink"), so the client's mic-capture/encode path needs no change.

Independent reimplementation (not importing gateway code) because agent-assistant and
`apps/api_gateway` are separately deployed components; a ~15-line header codec is cheap
enough that duplicating it is simpler than introducing a shared package dependency.

### `ws_protocol.py`

Endpoint changes from `/v1/conversation/stream?<query params>` to `/v1/lugo/stream` with
no query parameters — all per-connection config (profile, audio params, session id,
feature flags) moves into the `wakeup` JSON message body instead.

### `service.py` (largest change)

- **Handshake inverts**: today the server speaks first (`session_started` on connect,
  unprompted). Under lugo, the **client must speak first**: immediately after the WS opens,
  send
  `{"type":"wakeup","profile":<config.profile>,"session_id":<persisted id, if any>,"audio_params":{"sample_rate":...,"output_sample_rate":...},"features":{"mcp":true}}`,
  then await `welcome`.
- **Event mapping** (old `conversation.py` event → new lugo wire message):
  - `session_started` → `welcome` (+ new `stt_ready`/`tts_ready` fields)
  - `speech_start` / `speech_end` / `processing` → same-named new messages (Part 1 item 4)
  - `user_transcript` → `stt`
  - `response_text` + `audio_start` → `tts{state:"start"}` + `tts{state:"sentence_start", text}`
  - `turn_done` / `aborted` → `tts{state:"stop"}` (with `reason` on the aborted path, Part 1 item 4)
  - `error` → `error` (unchanged shape)
  - new, no old equivalent: `goodbye` (server-initiated idle disconnect — close cleanly),
    `engines_ready` (stop the warming-reminder tone), `mcp` (route to `mcp_tools.py`
    dispatcher, Part 3), `command` (log/ignore for now — unrelated passthrough channel, not
    used by any of the 4 MCP tools in this pass)
- **Downlink binary frames**: each incoming WS binary message is now `decode_frame()`'d
  first; the unwrapped Opus payload goes into `PlaybackBuffer` exactly as today. Non-Opus
  frame types are logged and dropped defensively.
- **Session resume**: unchanged persistence mechanism (`session_state.py`); the persisted
  id is now sent as the `session_id` field of `wakeup` instead of a `?session_id=` query
  param, and the id echoed back in `welcome` is persisted the same way as today.
- **Barge-in**: no change. `{"type":"abort"}` is sent the same way; `lugo.py` already
  handles `ctype == "abort"`.
- **STT pre-warm**: no change — `POST /v1/stt/warm?profile=` is a plain REST call,
  independent of which WS endpoint is used.

### `config.py` / `config.example.yaml`

Remove `session.output` — lugo hardcodes `want_audio=True, want_text=True` with no toggle,
so this field would have no effect. The config loader stays tolerant of the now-unused key
(ignore rather than error) so existing deployed `config.yaml` files don't break on
upgrade. All other fields (`audio.*`, `led.*`, `oled.*`, `service.*`,
`session.profile`/`allow_barge_in`/`barge_in_*`) are unchanged.

### Unchanged files

`audio_io.py` (aside from adding the volume-gain hook, Part 3), `playback_buffer.py`,
`led_status.py`, `oled_status.py` (aside from adding a custom-text method, Part 3),
`session_state.py`, `runner.py`.

### Protocol replacement, not addition

Per explicit decision: `/v1/conversation/stream` support, its JSON event handling, and the
`audio_out=url` WAV/URL fallback path are **deleted**, not kept behind a config flag. This
is a breaking change for this client — see rollout order below.

## Part 3 — MCP tool set (`agent-assistant/a2a_client/mcp_tools.py`, new file)

A minimal hand-rolled JSON-RPC 2.0 dispatcher mirroring the pattern in ESP32's
`mcp_server.c`: a static list of tool defs (`name`, `description`, `inputSchema`,
`annotations`) plus `dispatch(payload: dict) -> dict` handling `initialize`, `tools/list`,
`tools/call`. Chosen over the official `mcp` Python SDK because that SDK only ships
stdio/SSE/streamable-HTTP transports — none match the lugo wire's
`{"type":"mcp","payload":{...}}` JSON envelope over an already-open WebSocket, so adopting
it would still require writing a custom transport, at which point the SDK adds a
dependency without saving implementation work for a 4-tool surface. Responses match what
`DeviceMcpToolSource` (gateway) expects: `{"content":[{"text": "..."}]}` on success,
`{"isError": true, "error": "..."}` on failure.

Four tools, none requiring `annotations.requiresConfirm` (all are soft/reversible — no
`shutdown` or raw `gpio.set` in this pass, see Non-goals):

1. **`self.get_device_status`** → `{"volume": <0-100>, "uptime_s": <float>}`. (No
   free-heap figure — not meaningful on Linux.)
2. **`self.audio.set_volume`** → sets a **software gain** multiplier (0-100%) applied to
   decoded PCM in the playback path (`audio_io.py`/`playback_buffer.py`), *not* an OS-level
   ALSA mixer change. Rationale: mixer control names differ across sound cards/HATs (USB
   DAC vs HifiBerry vs onboard), so shelling into `amixer` risks silently targeting the
   wrong control or card. A software gain is card-agnostic and mirrors what ESP32's own
   `audio_set_volume` already does (gain before the codec, not an OS setting).
3. **`self.device.idle`** → mutes mic capture and shows an idle indicator on
   OLED/LED. Because this pass has no physical wake button on the RPi (Non-goals), the
   existing RMS-based barge-in detector (`barge_in_rms_threshold`/`barge_in_min_frames`,
   already used to interrupt playback) is **repurposed** as the wake trigger while idle —
   no new detection code, just a new use of the existing one.
4. **`self.screen.show_text`** → renders up to 2 lines of arbitrary text on the OLED via a
   new `oled_status.py` method, auto-reverting to the current state's normal text after a
   fixed ~5s timeout (same overlay-then-revert pattern ESP32 uses for its volume overlay).

MCP is unconditionally enabled (`features.mcp: true` hardcoded in the `wakeup` builder) —
no config toggle, since none of the four tools are destructive.

## Part 4 — Testing & rollout

### New RPi-side unit tests (pytest, alongside the existing 4 in `agent-assistant/tests/`)

- `test_lugo_frame.py` — encode/decode round-trip; short-header and payload-size-mismatch
  error cases.
- `test_mcp_tools.py` — `initialize`/`tools/list`/`tools/call` for each of the 4 tools,
  plus unknown-tool and malformed-payload cases.
- Update `test_ws_protocol.py` for the new endpoint/handshake (no query params, `wakeup`
  body shape). `test_session_state.py` and `test_playback_buffer.py` need no changes (the
  persisted value and buffer semantics are unchanged).

### Gateway-side tests (repo root `tests/unit/`, alongside `test_lugo_stream.py`,
`test_lugo_idle_timeout.py`, `test_lugo_device_mcp.py`)

Add cases for: `resume_sid` threaded from `wakeup.session_id`; `stt_ready`/`tts_ready` in
`welcome`; `engines_ready` forwarded; `speech_start`/`speech_end`/`processing`/`aborted`
reason forwarded; confirm an ESP32-style `wakeup` with no `session_id` field is unaffected
(still gets a fresh uuid4, `resume_sid=None`).

### Manual validation (no Wokwi-equivalent simulator exists for RPi)

Checklist against a real Pi + staging gateway: wakeup→welcome handshake succeeds;
voice-driven volume change via the LLM; idle then wake via loud sound; `show_text` renders
and auto-reverts; session resumes correctly across a manual client restart.

### Rollout order

Gateway (`lugo.py`) changes ship first — additive/backward-compatible, safe for the
existing ESP32 fleet. Only once that's deployed does the new RPi client build get rolled
out, since it hard-depends on the new `/v1/lugo/stream` behavior and no longer speaks
`/v1/conversation/stream` at all.
