# Web audio streaming jitter fix — design

## Problem

Web client playback (`lugo-web-client/src/audio/player.ts`) stutters — audible gaps,
choppy audio — even on a good WiFi connection. STT/mic capture direction is not
affected; this is playback (server→client Opus audio) only.

## Root cause

`_stream_to_tts` (`apps/api_gateway/app/services/conversation/session.py:599-671`)
paces outgoing Opus frames on a **global real-time clock**: after an initial
`conversation_opus_prebuffer_frames` frames (default 5 × 60ms = **300ms**), it
releases exactly one frame per 60ms of wall-clock time
(`system_config.py: conversation_opus_pace`, `conversation_opus_prebuffer_frames`).

300ms of slack is sized for ESP32/RPi's small physical audio ring buffers, which
need a steady, capped-rate drip-feed to avoid overflow. Browsers have no such
constraint — Web Audio (`AudioContext`) can hold many seconds of scheduled audio
— but they inherit the same 300ms cushion. Any small hiccup that exceeds it (GC
pause, tab throttling, ordinary network jitter — no packet loss required) drains
the client buffer. `player.ts`'s `nextStartTime()` then has to "catch up to now",
producing an audible gap. This reproduces on good networks because the trigger is
JS-main-thread / scheduling jitter, not bandwidth or loss.

## Goals

- Eliminate playback stutter on the web client under normal network conditions.
- Zero behavior change for ESP32 / RPi devices (`docs/device-integration.md`),
  which are working correctly today.
- Minimal, additive change — no new client-server protocol messages, no adaptive
  feedback loop (see "Rejected approaches").

## Non-goals

- Mic/upload-direction robustness (not the reported symptom).
- WS reconnect/resume on drop (separate concern; not requested here).
- Native mobile app (future phase, tracked separately) — this design should not
  make that harder, but does not implement it.

## Design

### Why disabling server-side pacing is sufficient

Opus packets for one sentence are fully encoded into an array
(`packets = opus_encoder.encode_pcm16(pcm)`) **before** the per-packet send loop
starts — pacing only throttles the *send* of already-ready data, it does not
change when synthesis finishes. So:

- Time-to-first-audio-byte is identical whether paced or not.
- With pacing off, a whole sentence's audio (frequently several seconds) is
  pushed to the socket back-to-back. The client decodes and schedules all of it
  via `AudioBufferSourceNode.start(at)` immediately — Web Audio happily queues
  seconds of audio ahead of `ctx.currentTime`. That queued-ahead margin *is* the
  jitter buffer; no explicit client buffer bookkeeping needs to be built.
- `conversation_tts_lookahead` (default 3 sentences) keeps synthesis running
  ahead of playback, so by the time one sentence finishes playing the next is
  normally already encoded and ready to burst out — pacing was, if anything,
  *delaying* delivery of data that was already sitting there ready.

Net effect of turning pacing off for the web session: the client naturally
accumulates a multi-second cushion instead of a fixed 300ms one, with no new
state machine.

One residual gap: the very first packet of a turn starts at `ctx.currentTime`
with zero lead (`cursor` is 0), so main-thread jank at that single moment isn't
cushioned by "already queued ahead" — because nothing is queued yet. Fix: a
small fixed startup delay (~100ms) applied only when scheduling the first chunk
of a turn.

### Server change (`apps/api_gateway`)

- `services/conversation/session.py`: `SessionRuntimeConfig` gains one field:
  `opus_pace: bool | None = None`. `None` = "not specified, inherit the global
  `system_config.conversation.conversation_opus_pace`" — this is the default for
  every existing caller, including `lugo.py`.
- `_stream_to_tts`'s pacing decision (`session.py:613`) becomes:
  `_do_pace = cfg.opus_pace if cfg.opus_pace is not None else _conv_cfg.conversation_opus_pace`.
  Same code path, same loop — one line changes what feeds `_do_pace`.
- `api/routes/conversation.py` (web's `/v1/conversation/stream`): parse a new
  optional query param `opus_pace` (reuse the existing `_truthy` helper already
  used for `denoise`), pass it into `SessionRuntimeConfig`.
- `api/routes/lugo.py` (ESP32/RPi's `/v1/lugo/stream`): **no change**. It builds
  `SessionRuntimeConfig` without `opus_pace`, so the field defaults to `None` and
  pacing behavior is byte-for-byte identical to today.
- `api/routes/livehost.py` has its own, separate Opus-pacing implementation
  (`pacing_delays()`, unrelated TikTok live-hosting feature) — out of scope,
  not touched.

### Client change (`lugo-web-client`)

- `src/audio/conversation.ts` `buildParams()`: always send `opus_pace=0` — the
  web client opts itself into the new behavior; nothing server-side changes by
  default.
- `src/audio/player.ts`: add a fixed ~100ms startup delay applied only when
  `this.cursor === 0` (first chunk of a turn), inside `schedule()`/
  `nextStartTime()`. No other scheduling logic changes — the existing
  tail-to-tail queuing and catch-up-to-now fallback are unaffected and still
  needed as a safety net for genuinely large stalls.

### Safety / ESP32-RPi non-impact

Verified by reading both WS route handlers: `conversation.py` (web) and
`lugo.py` (device) each independently construct `SessionRuntimeConfig` and pass
it to the same `ConversationSession`. The new field is opt-in and additive;
`lugo.py` has no knowledge of it, so device sessions keep resolving pacing from
`system_config_store().conversation.conversation_opus_pace` exactly as before.
No shared mutable state is introduced — the override lives on the per-connection
dataclass instance, not on the global `system_config_store` singleton.

## Edge cases

- Inter-sentence gaps (TTS synthesis slower than playback of the prior sentence)
  are a `conversation_tts_lookahead`/synthesis-speed concern, orthogonal to this
  change. Disabling pacing cannot make this worse (see "why sufficient" above) —
  it removes an artificial throttle on already-ready data, it doesn't slow
  synthesis down.
- A session with `audio_out != "opus"` (browser `audio_url` fallback, or
  `libopus` unavailable server-side) never reaches the pacing branch at all —
  `opus_pace` is inert in that mode.
- If a future device client accidentally sent `opus_pace=1`/`0` on its own
  (nothing does today), it would only turn the existing global pacing behavior
  on/off for itself — never crashes, never touches other sessions.

## Testing

- Server unit test: `SessionRuntimeConfig(opus_pace=False, ...)` → `_stream_to_tts`
  sends all packets with no `asyncio.sleep` (mock the clock / count sleep calls).
- Server unit test (regression): `opus_pace=None` (or a `lugo.py`-style config
  that never sets it) → pacing behavior unchanged from before this change.
- Web client unit test: `buildParams()` includes `opus_pace=0`.
- Manual verification: Chrome DevTools network throttling (mid-range jitter
  profile) on a live web conversation, before/after.

## Rejected approaches

- **Only raise `conversation_opus_prebuffer_frames` for web** (larger fixed
  cushion, keep real-time pacing). Simpler, but still has a hard ceiling —
  jitter beyond the new cushion still stutters. Rejected in favor of removing
  the artificial ceiling entirely, since the fix is no more invasive.
- **Adaptive jitter buffer with client-reported buffer depth / RTT feedback.**
  Correct in theory for highly variable bandwidth, but Opus mono ~24kbps is
  small enough that this is unjustified complexity (new protocol messages,
  server-side adaptation logic) without concrete evidence it's needed. Revisit
  only if the fix above proves insufficient in the field.

## Follow-ups (not in this change)

- Mic/upload-direction resilience and WS reconnect-on-drop were not reported as
  broken; out of scope here but worth a look if "mất gói" (dropped packets)
  turns out to mean something beyond playback stutter once this ships.
- Native mobile app: this design keeps the per-connection override on the wire
  protocol (query param), so a future mobile client can opt in the same way the
  web client does — no protocol redesign needed for that phase.
