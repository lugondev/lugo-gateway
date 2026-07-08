# Lugo device protocol — design

**Date:** 2026-07-08
**Status:** Approved (design)
**Scope:** Phase 1. Phase 2 items are noted but specced separately later.

## Problem & goal

The ESP32/RPi/agent voice clients and the browser control panel all talk to the
gateway over a bespoke WebSocket protocol at `WS /v1/conversation/stream`
(`{"event": ...}` JSON + query-param config + raw-opus binary frames, server-side
VAD, always-streaming). We want to converge every client onto **one** device
protocol, learning from the design of `78/xiaozhi-esp32` (hello/listen/tts/abort,
binary framing, client-driven turns, device identity, connect-on-wake lifecycle)
**without using the "xiaozhi" name**.

The unified protocol is named **Lugo**. End state: a single Lugo protocol across
gateway, ESP32, agent, RPi, and browser; the legacy `/v1/conversation/stream`
endpoint is retired once all clients migrate.

Key differences from today that motivate this:
- **Connect-on-wake lifecycle** instead of always-streaming (power/bandwidth).
- **Barge-in** that stops the bot's turn without dropping the connection.
- **Idle disconnect** to a sleep state, timeout configured per profile.
- **Profile-as-identity**: the device sends only a profile id; the server resolves
  LLM / TTS / system prompt / MCP / memory (and default STT) from it.

## Non-goals (Phase 1)

- Wake-word detection on device (ESP-SR) — Phase 2. Phase 1 uses button + local
  triggers and **server-side VAD** for turn segmentation (auto mode).
- Remote-call / server-initiated wake — Phase 2. Requires an always-on channel
  (e.g. MQTT) because the WS is closed while idle. Phase 1 only reserves protocol
  room for it.
- MQTT+UDP transport — Phase 2. Phase 1 is WebSocket only.
- `llm{emotion}` (facial expression) messages, JSON-over-binary frames — reserved.
- Refactoring `livehost.py` to share the core — out of scope (noted as a future
  beneficiary of `ConversationSession`).

## Architecture

One shared core, thin per-protocol front-ends. **No duplicated pipeline.**

```
   ESP32 ─┐        gateway (FastAPI)
   RPi   ─┤ Lugo   ┌────────────┐   ┌─────────────────────┐
   agent ─┼──WS──▶ │ lugo front │──▶│ ConversationSession  │
   browser┘        │ end        │◀──│  (CORE)              │
                   └────────────┘   │  profile/engine      │
   browser(old) ─┐ ┌────────────┐   │  resolve, endpointer │
   RPi(old)     ─┼▶│ event front│──▶│  VAD, handle_turn    │
   agent(old)   ─┘ │ end (shim) │◀──│  STT→LLM→TTS, MCP,   │
                   └────────────┘   │  persist, memory,    │
                                    │  barge-in            │
                                    └─────────────────────┘
```

- **`ConversationSession` (new core, extracted from `conversation.py`)** — all
  protocol-neutral logic. Communicates via a neutral callback `emit(event, **payload)`
  and inbound methods `feed_audio(pcm)`, `feed_text(text)`, `abort(reason)`,
  `close()`. Emits neutral events only (no wire naming): `session_started`,
  `speech_start`, `speech_end`, `transcript`, `response_text`, `tts_start`,
  `audio(pcm)`, `tts_end`, `turn_done`, `command`, `error`.
- **`lugo` front-end (new)** — route `WS /v1/lugo/stream`. Translates neutral
  events ↔ Lugo wire (`{"type": ...}` + v3 binary framing). Target protocol for
  ESP32/RPi/agent/browser.
- **`event` front-end (temporary shim)** — `conversation.py` reduced to a thin
  adapter that reproduces the current `{"event": ...}` wire exactly, so the
  existing browser/RPi/agent keep working during migration. Retired at the end.

Both front-ends drive the *same* `ConversationSession`.

## Protocol (Lugo, Phase 1)

**Channels:** JSON control on WebSocket **text** frames; opus audio on **binary**
frames wrapped in a v3 header.

**v3 binary header (4 bytes, before payload):**
```c
struct LugoFrame { uint8_t type; uint8_t reserved; uint16_t payload_size; uint8_t payload[]; }
// type: 0 = OPUS audio, 1 = JSON (reserved; Phase 1 sends JSON on text frames)
```

**Client → Server**

| type | payload | meaning | maps to core |
|---|---|---|---|
| `wakeup` | `{version, profile:"esp32-assistant", trigger:"button"\|"wake_word"\|"local_call"\|"remote_call", audio_params:{format:"opus",sample_rate:16000,frame_duration:60}, features:{}}` | handshake; declares profile + wake trigger | replaces query params |
| `listen` | `{state:"start"\|"stop"\|"detect", mode:"auto"\|"manual", text?}` | turn/listen control (Phase 2 wake word). Phase 1: `auto` = server VAD | — |
| `abort` | `{reason:"wake_word_detected"\|"user"}` | **barge-in**: stop bot turn, keep connection | `session.abort()` |
| `text` | `{text}` | text-input turn (browser/test) | `feed_text()` |
| *(binary)* | v3 type=0 opus | mic audio up | `feed_audio()` |

**Server → Client**

| type | payload | maps to core |
|---|---|---|
| `welcome` | `{session_id, transport:"websocket", audio_params:{sample_rate:<output>}, idle_timeout_s:<from profile>}` | `session_started` |
| `stt` | `{text, final:bool}` | `transcript` |
| `tts` | `{state:"start"\|"sentence_start"\|"stop", text?}` | `tts_start`/`response_text`/`tts_end` |
| `mcp` | `{...}` (JSON-RPC) | `command` |
| `error` | `{message}` | `error` |
| `goodbye` | `{reason:"idle_timeout"}` | server-initiated idle disconnect |
| *(binary)* | v3 type=0 opus | reply audio |

`welcome` carries `idle_timeout_s` so the device can set its watchdog from the
profile rather than hardcoding it.

## Connection lifecycle & state machine

Device states: **SLEEP** (no WS) → **CONNECTING** → **LISTENING** ⇄ **SPEAKING** → SLEEP.

1. **SLEEP** — WS closed, radio idle. Wake-word detector (Phase 2) / button wait.
2. **wakeup** (button / local trigger / Phase 2 wake word) → open WS → send
   `wakeup{profile,...}` → receive `welcome{session_id, idle_timeout_s}` →
   **LISTENING**, start streaming mic (opus, v3-wrapped).
3. User speaks → server VAD endpoints the utterance (Phase 1 auto mode) →
   STT→LLM→TTS → server pushes `tts{start}` + audio → **SPEAKING** (mic muted,
   half-duplex).
4. **Barge-in** — while SPEAKING, pressing wakeup → client sends
   `abort{reason:"wake_word_detected"}` → server cancels the turn and stops
   pushing audio → back to **LISTENING**. **Connection stays open.**
5. **Idle timeout** — after `idle_timeout_s` with no interaction (no speech, no
   turn, no audio playing) → **server** sends `goodbye{reason:"idle_timeout"}`
   and closes the WS → device → **SLEEP**.
6. wakeup again → step 2.

**Idle timeout ownership:** the **server** is the source of truth (it knows turn
and VAD state); the timer resets on activity (speech_start / new turn / audio
playing). The device runs a **secondary watchdog** (no signal for
`idle_timeout_s + grace` → self-sleep) to survive a silently dropped WS.

**Button semantics by state:** SLEEP → wakeup/connect; SPEAKING → barge-in;
LISTENING → no-op (or manual end-of-turn, optional).

## Profile changes

`services/profiles/models.py`:
```python
class SessionConfig(BaseModel):
    idle_timeout_s: int = 30   # 0 = never auto-disconnect

class Profile(BaseModel):
    ...
    session: SessionConfig = SessionConfig()
```
Add `session` to `ProfileRequest` (`routes/profiles.py`) for CRUD/UI. Existing
`profiles.json` without the field defaults to 30s — no breakage.

**Note (STT nuance):** the profile resolves LLM/TTS/system_prompt/MCP/memory, but
STT engine currently comes from server global settings
(`conversation_stt_engine`/`default_stt_engine`), not the profile JSON. Phase 1
keeps this. A per-profile `stt` field is a small future addition if needed.

## Server implementation

1. **`Profile.session`** as above.
2. **Extract `ConversationSession`** → new `services/conversation/session.py`. Move
   protocol-neutral logic out of `conversation.py`: profile/engine resolution,
   endpointer VAD, `handle_turn` (STT→LLM→TTS via `_stream_to_tts`), persist /
   memory / MCP, barge-in.
3. **`event` front-end** = `conversation.py` reduced to a thin adapter producing
   the current `{"event": ...}` wire unchanged (regression-guarded by the 314
   existing tests).
4. **`lugo` front-end** = new `routes/lugo.py` at `WS /v1/lugo/stream`: parse
   `wakeup` → resolve profile → create `ConversationSession` → send `welcome`;
   translate neutral events → `{"type": ...}` + v3 frames; binary v3 →
   `feed_audio`; `abort` → `session.abort("barge-in")` (keep WS); `text` →
   `feed_text`.
5. **Idle timeout** lives in the `lugo` front-end (not the core — the browser on
   the event shim must not be disconnected). `asyncio` timer reset on activity;
   on expiry → `goodbye` → close.
6. **v3 frame codec** = `services/conversation/lugo_frame.py`:
   `encode(type, payload)` / `decode(bytes) -> (type, payload)`. Pure functions,
   host-testable.

## Firmware implementation (esp32-assistant)

Keep hardware/audio components; swap the protocol layer and add the controller.

1. **New component `lugo_protocol`** (replaces `ws_protocol`): build/parse
   `wakeup`/`welcome`/`listen`/`abort`/`text` + v3 encode/decode. Host-tested like
   the existing `test_ws_protocol.c`.
2. **State machine** (in `main.c` or a `session_fsm` component) implementing the
   lifecycle above: SLEEP→CONNECTING→LISTENING⇄SPEAKING→SLEEP.
3. **Triggers (Phase 1):** button (`buttons` component) + local command.
   Phase 2: ESP-SR wake word (runs only in SLEEP to save power).
4. **Config (`Kconfig.projbuild`):** drop `AA_STT_ENGINE`/`AA_TTS_ENGINE` (server
   owns via profile); `AA_PROFILE` becomes required; keep
   `AA_SERVER_HOST/PORT/SECURE`, I2S/I2C pins. `idle_timeout` comes from the
   server `welcome`, not device config.
5. **Watchdog** driven by `idle_timeout_s` from `welcome`.

## Error handling

- `wakeup` missing/unknown profile → server sends `error{message}` then closes;
  device returns to SLEEP and may retry. Never silent.
- Opus decode error → drop the packet, continue (as in the current core).
- WS drop mid-turn → device → SLEEP; watchdog + `goodbye` protect both ends.
- `abort` with no active turn → safe no-op.
- v3 frame with bad/oversized `payload_size` → drop frame, log, no crash.

## Testing

- Core `ConversationSession`: unit tests with stub STT/TTS (reuse existing fixtures).
- `lugo_frame`: unit encode/decode + edges (size 0, overflow, unknown type).
- Lugo route: `websocket_connect` tests — wakeup→welcome, one audio turn→tts,
  abort mid-turn, idle timeout→goodbye.
- Firmware `lugo_protocol`: host-test build/parse (like `test_ws_protocol`).
- Regression gate: the 314 existing tests must stay green after the core
  extraction (this is the safety net for server step 1).

## Rollout order

Each step ships green before the next.

1. **Gateway** — extract `ConversationSession`, keep `event` shim (314 tests
   green), add `/v1/lugo/stream` + `lugo_frame` + idle timer + `Profile.session`.
   No client breaks (Lugo is a new endpoint).
2. **ESP32** — `lugo_protocol` + FSM; host-test then flash. First real Lugo client.
3. **agent** — migrate `agent-assistant/a2a_client/ws_protocol.py` to Lugo.
4. **RPi** — migrate `scripts/rpi_voice_client.py` to Lugo.
5. **Browser** — migrate `conversation.js` + `chat.js` to Lugo (final test client).
6. **Retire** the `event` shim and `/v1/conversation/stream` once unused.

During steps 1–5 both endpoints coexist on the shared core; migration is
incremental, not big-bang.

## Phase 2 (separate spec later)

- ESP-SR wake word; client-driven `listen{start/stop/detect}`.
- Remote-call: always-on channel (MQTT) so the server can wake an idle device.
- Reserved now: `wakeup.trigger` field and the `listen` message keep Phase 1
  forward-compatible.
