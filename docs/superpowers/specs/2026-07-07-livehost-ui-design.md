# Livehost (TikTok Co-host) UI — Design Spec
**Date:** 2026-07-07

## Overview

The TikTok co-host feature (`docs/superpowers/specs/2026-07-05-livehost-tiktok-cohost-design.md`) shipped as backend-only: `EventScheduler`, `TikTokLiveIngestor`, `LiveHostOrchestrator`, and the `/v1/livehost/*` REST + WS API, with zero corresponding frontend. This spec adds the missing browser UI — a new sidebar section mirroring the existing Voice→Voice conversation pane, so the feature is actually usable from the web UI as the original design intended ("Output can play through the browser UI").

## Architecture

New sidebar section `section-livehost` (nav item `data-section="livehost"`) and a new, **independent** JS module `static/js/livehost.js`. It mirrors `conversation.js`'s structure (WS lifecycle, mic capture, gapless audio playback, event-driven UI updates) but does **not** import from or share state with `conversation.js`/`chat.js` — those two are already tightly coupled to each other (shared `conv` object, shared `#chat-dialogue`) because they're sub-modes of one Chat session; livehost is a wholly separate session type running on a separate WS connection (`/v1/livehost/stream` vs `/v1/conversation/stream`), so sharing state would let one interfere with the other. This mirrors the backend's own choice to duplicate rather than merge `conversation.py`/`livehost.py`.

## Components

### TTS/STT & TikTok connect bar

- STT engine select (`lh-stt-engine`, populated like `conv-stt-engine`).
- TTS Profile select (`lh-tts-profile`, populated from the existing `ttsProfileData` — add a `renderLivehostTtsProfileSelect()` export to `tts-profiles.js`, called from `loadTtsProfiles()` alongside the two existing render calls).
- TikTok username input (`lh-tiktok-username`) + Connect/Disconnect buttons — disabled until the session WS is open.
- Two independent lifecycle controls, since they map to two independent backend calls: **Start/Stop Session** (opens/closes the WS, like `conv-start`/`conv-stop`) and **Connect/Disconnect TikTok** (`POST/POST /{session_id}/connect|disconnect`, only meaningful once the session is open).

### Status

- Session status (`lh-status`): idle / starting / listening — reuses the same `status-idle`/`status-rec`/`status-error` classes as `conv-status`.
- TikTok connection badge (`lh-tiktok-status`): idle/connecting/live/reconnecting/offline_waiting/error, one badge per `IngestorState` value. The backend does not push state-change events over the WS, so the UI polls `GET /{session_id}/status` every 2s while a session is open and TikTok is connected, stopping the poll on disconnect/session-stop.

### Event log

A single scrolling feed (`lh-dialogue`) combining, in arrival order:
- `user_transcript` → a "streamer" bubble (same visual style as `addBubble("user", ...)` in conversation.js, reimplemented locally).
- `social_event` → a lightweight feed row (not a bubble): `[kind] user_name: text` for comments, `user_name gifted gift_name (value)` for gifts, `user_name followed` / `user_name liked` for the rest.
- `response_text` → an "assistant" bubble, same as conversation.js.
- `social_reply` → not rendered as its own row; it only tags the *next* assistant bubble with a small "↳ replying to chat" marker so the viewer can tell a voice-triggered reply from a social-triggered one.
- Audio playback reuses the exact gapless-scheduling approach from `conversation.js` (`convEnqueueAudio`'s pattern), reimplemented against local state (`lh.ctx`, `lh.nextTime`, `lh.sources`) rather than the shared `conv` object.

## Data Flow

1. User picks STT engine + TTS Profile, clicks **Start Session** → mic permission requested (`createMicCapture`, reused as-is from `audio-capture.js`) → WS opens to `/v1/livehost/stream?stt_engine=...&tts_profile=...&session_id=...` → on `session_started`, UI shows "listening".
2. User types a TikTok username, clicks **Connect** → `POST /{session_id}/connect {unique_id}` → UI starts the 2s status poll → badge updates as the ingestor transitions `connecting → live` (or `→ error`).
3. From here the WS drives everything else identically to conversation.js's `ws.onmessage` switch, plus the two new event types (`social_event`, `social_reply`) appended to the feed as described above.
4. **Disconnect** → `POST /{session_id}/disconnect`, stop the status poll, badge → idle; the voice session (mic, WS) stays open — disconnecting TikTok must not kill the co-host's ability to talk, per the original spec's core constraint.
5. **Stop Session** → same teardown as `conv-stop` (send `{type:"end"}`, close WS, stop mic capture) — also implicitly ends any TikTok connection since the backend's `finally` block calls `ingestor.stop()` on WS teardown regardless.

## Error Handling

- WS `error` events render as a red status line, same as conversation.js — matches the spec's rule that voice-turn failures surface directly to the streamer.
- A failed `POST /connect` (e.g. bad username) shows an inline error under the TikTok input; it does not affect session state, so the streamer can retry with a different username without restarting the session.
- If the status poll's `GET /status` 404s (session gone — e.g. WS dropped unexpectedly), the poll stops itself and the TikTok badge resets to idle; it does not throw.

## Testing

No JS test framework exists in this repo (per the TTS Profile UI work). Verification: `node --check` on all new/touched JS files, plus a curl-based smoke check of the new HTML markup and REST endpoints, mirroring how the TTS Profile UI task was verified. A manual browser click-through (start session → connect a TikTok username → observe the feed) is recommended before considering this done, same caveat as before.

## Out of Scope

- Per-gift-type visual treatment (icons, animations) — plain text rows for now.
- Social event history / replay after a session ends.
- Multiple simultaneous TikTok rooms per session (matches the backend spec's own out-of-scope).
- Any change to `conversation.py`/`livehost.py` backend behavior — this is UI-only, consuming the existing API exactly as it stands today.
