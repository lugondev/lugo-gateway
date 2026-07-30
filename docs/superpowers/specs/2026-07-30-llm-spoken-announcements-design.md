# LLM-spoken announcements: new session and pre-idle goodbye

**Date:** 2026-07-30
**Status:** approved, ready to implement

## Problem

Two moments end a conversation, and neither says so in the assistant's own words.

**New session.** `new_session` rotates the conversation record. On the voice path the
model happens to confirm it out loud, because the device asks via the
`self.session.new` MCP tool and the gateway now lets that turn finish. Every other
path — the web "New conversation" button, a bare `new_session` frame, a hardware
button later — rotates in silence. The user has no signal that anything happened.

**Pre-idle goodbye.** The device path speaks `system_config.conversation_goodbye_text`
before disconnecting: one fixed phrase ("Hẹn gặp lại nha!"), the same every time,
identical across every profile no matter what persona that profile defines.

## Decisions

| Question | Decision |
|---|---|
| How is the line produced? | A live LLM call per event, **with conversation context** — it can refer to what was just said. |
| The voice path already confirms — announce anyway? | No. Announce only when nobody has. |
| LLM or TTS unavailable? | Stay silent, and emit an `error` naming the stage so the device display shows *why*. |
| Do the lines land in history? | Yes, both. |
| Keep the admin phrase as a fallback? | No — `conversation_goodbye_text` is removed entirely. |

## Design

### `services/conversation/announce.py` (new)

One pure-ish function, testable without a session:

```python
async def generate_line(*, responder, persona, history, language, event) -> str
```

- `event` is `"new_session"` or `"idle_goodbye"`; it selects the directive.
- Messages sent to the LLM: `persona` (the profile's resolved system prompt, so the
  line stays in character) plus a short output contract — one sentence, spoken aloud,
  no quotes, no emoji, in the user's language; then the **last 6 messages** of
  `history` (capped at ~1200 characters, oldest dropped first) as context; then the
  directive.
- `language` pins the output language when the profile sets one. When it is empty the
  directive tells the model to mirror the language of the context instead — which is
  why context is worth sending even for a generic line.
- The reply is cleaned before returning: first non-empty line, surrounding quotes
  stripped, truncated to 200 characters at a word boundary.
- Calls `responder.reply()` — the profile's own LLM, over the connection the session
  already holds. No new client, no separate credentials path.

`session.py` is already ~1100 lines; prompt construction and output cleaning are the
parts most worth testing in isolation, so they live in their own module.

### `ConversationSession.announce(event)`

1. Skip when there is nothing to speak with: no responder, no TTS provider, or
   `not cfg.want_audio` (a text-only session has no announcement to make).
2. `generate_line(...)`. On failure: `emit("error", message="llm: …")`, return.
3. `_persist("assistant", line)` and append to `self.history`, so History shows what
   was said and the model knows it already greeted.
4. `speak(line)`. `speak()` gains a return value: `None` when the utterance was
   spoken or deliberately skipped (empty text, text-only session, over quota), or a
   short reason string when synthesis genuinely failed. On a reason:
   `emit("error", message="tts: …")`.

The device needs no firmware change: it already renders an `error` event as
`Error / <text>` on the panel, which is how "silent because TTS broke" becomes
visible.

### Rotation hook

`rotate(reason, announce: bool = False)`.

- `_rotate_when_turn_ends` (the deferred path — a voice tool asked from inside a
  turn, and that turn confirmed it) passes `announce=False`.
- `request_rotate`'s immediate path (button, bare frame) passes `announce=True`.

The announcement runs after `session_rotated` is emitted, so it is persisted under
the new session id — it is that conversation's first utterance.

### Idle goodbye

`lugo.py`'s watchdog drops the `conversation_goodbye_text` lookup and calls
`await session.announce("idle_goodbye")`, keeping the existing 0.5s drain before
`goodbye`. The line is persisted under the session that is ending.

The LLM call adds roughly 0.5–1.5s before the goodbye. The device's own idle timeout
is the server's value plus a grace window, so the server still fires first — to be
confirmed on hardware, not assumed.

### Removing the config

Delete `conversation_goodbye_text` from `ConversationSettings` and its only use in
`lugo.py`. A stored `config_system` row that still carries the key must keep loading
(unknown keys ignored) — covered by a test. Historical docs and plans keep their
mentions; they are a record of what was true then.

## Cost

200–400 tokens per event, and only on the paths that would otherwise be silent:
non-voice rotations and idle disconnects.

## Testing

- `announce.py`: prompt carries persona and context; context is capped and trimmed
  oldest-first; output cleaning (quotes, multi-line, over-long); language pinned when
  set, mirrored when not.
- `announce()`: persists, appends to history, speaks; LLM failure → `error` mentioning
  `llm` and no speech; TTS failure → `error` mentioning `tts`.
- Rotation: immediate rotation announces; deferred rotation does not.
- Idle: the watchdog speaks a generated line before `goodbye`.
- Config: no references to `conversation_goodbye_text` remain, and a legacy row
  containing it still loads.

## Hardware verification

1. Web "New conversation" → a fresh in-character line is heard.
2. Device idle 20s → a goodbye is heard before the disconnect.
3. Say "bắt đầu lại" to the device → exactly one confirmation, not two.
