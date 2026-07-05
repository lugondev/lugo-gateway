# Livehost: TikTok Live AI Co-host — Design

## Purpose

An AI co-host feature for TikTok livestreams. It answers viewer comments and
gifts by voice (LLM + TTS), the same way a real co-host reads out chat during
a stream — while also handling the streamer's own spoken input (STT), exactly
like the existing Conversation loop. Output can play through the browser UI
or push to ESP32/Raspberry Pi voice devices, reusing the existing device
audio transport.

Out of scope for this design: routing AI audio into OBS / a virtual audio
cable so it plays on the actual live broadcast; platforms other than TikTok;
multiple simultaneous TikTok rooms per streamer.

## Architecture

```
TikTok Live room
      │ (unofficial WS, via the TikTokLive python library)
      ▼
TikTokLiveIngestor ──normalize──▶ SocialEvent
                                       │
                                       ▼
                                 EventScheduler (priority queue + adaptive batching)
                                       │
Voice input (browser mic / ESP32 / RPi) ──VAD/STT (reused)──▶ LiveHostOrchestrator ◀──┘
                                       │
                             base context (system_config_store)
                                       │
                               responder (LLM, reused)
                                       │
                               tts.service (reused)
                                       │
                                       ▼
                  WS /v1/livehost/stream
                  ├─ browser client  → audio_url (same as conversation today)
                  └─ ESP32 / RPi     → Opus binary frames (?audio_out=opus, reuses conversation's transport)
```

New code lives in `app/services/livehost/` (`schemas.py`, `ingestor.py`,
`scheduler.py`, `orchestrator.py`) plus a new route
`app/api/routes/livehost.py`. `conversation.py` and `responder.py` are not
modified beyond exposing existing helpers (`resolve_system_prompt`,
`build_responder_ex`) for reuse — this keeps the feature isolated from the
already-stable Conversation voice loop.

## Components

### SocialEvent schema

```python
class SocialEvent(BaseModel):
    id: str                          # uuid, for dedup
    platform: Literal["tiktok"]      # room to extend to other platforms later
    kind: Literal["comment", "gift", "like", "follow", "share"]
    user_id: str
    user_name: str
    user_avatar_url: str | None = None
    text: str | None = None          # comment body
    gift_name: str | None = None
    gift_value: int | None = None    # coin value, drives priority
    like_count: int | None = None    # TikTok batches likes; aggregate count
    timestamp: float
```

### TikTokLiveIngestor

Wraps `TikTokLiveClient` from the `TikTokLive` python library. `start(unique_id)`
connects to a room; handlers for comment/gift/like/follow events normalize
into `SocialEvent` and push onto an `asyncio.Queue` the `EventScheduler`
consumes. One ingestor instance per livehost session (1 session ↔ at most 1
TikTok room).

**Connection state machine** (exposed via `GET /v1/livehost/{session_id}/status`):

```
idle → connecting → live → reconnecting → offline_waiting → error
```

Reconnect rules — the core design constraint is that **losing the TikTok
connection must never kill the livehost session**; voice/TTS keeps working
regardless of ingestor state:

- Transient network errors: exponential backoff with jitter (1s → 2s → 4s →
  ... capped at 60s); backoff resets to zero once an event is received after
  reconnecting.
- Room ended (streamer went offline): don't hot-loop retries — switch to a
  slow poll (30–60s) waiting for the streamer to go live again, since
  aggressive reconnects against an offline room risk being rate-limited by
  TikTok's unofficial API.
- Explicit `POST /v1/livehost/{session_id}/disconnect`: stop cleanly, no
  further retries.
- Watchdog: if state is `live` but no event has arrived for N minutes (a
  connection that died without emitting a clean disconnect event), force a
  reconnect.
- A generation counter guards against overlapping reconnects creating two
  concurrent `TikTokLiveClient` instances for the same session.
- TikTok does not replay comment history, so there is no resume/offset
  logic — a connection gap is simply a gap. It may optionally be logged so
  the LLM can mention it ("looks like chat dropped for a bit") but that's not
  core behavior.

### EventScheduler

Priority score per `SocialEvent` at enqueue time:

1. Comment mentions the bot's name / an activation keyword
2. `gift`, weighted by `gift_value` — high-value gifts are never dropped
3. `follow`
4. Ordinary `comment`
5. `like` — aggregated context only; does not create its own turn by default

Dequeue decision happens at *consumption* time (`next_turn()`), not enqueue
time, since backlog size changes constantly:

- Backlog ≤ threshold (e.g. 3 response-worthy events): pop the single
  highest-priority event; the LLM replies addressing that user by name.
- Backlog > threshold: pop the top-K by priority and summarize the remaining
  count into one combined prompt ("Long sent a gift combo, and 12 others
  asked about X...") → a single TTS turn.
- Hard queue cap (e.g. 200 events): when full, drop the lowest-priority
  `comment`/`like` entries first; gifts and mentions are never dropped.

### LiveHostOrchestrator

One instance per session, turn loop:

```
loop:
  if VAD detects the streamer currently speaking → run a voice turn
     (VadEndpointer → STT → history), same as Conversation today
  elif streamer is silent (dead air) and scheduler.has_pending()
     → scheduler.next_turn() (single event or batch)
  else → wait
  → messages = [base_context (system_config_store), ...history, current_turn]
  → responder.reply_stream(messages) → tts.service → play audio
    (audio_url for browser, Opus frames for ESP32/RPi)
  → append to history: assistant reply as role="assistant"; a social turn is
    recorded as role="user", content="[TikTok @user_name] <text or gift
    description>" — reuses the existing history/session_store schema
    unchanged.
```

Voice always takes priority over social turns — comments/gifts never
interrupt the streamer talking, matching the existing barge-in behavior for
voice.

Persona / base context: reuses `system_config_store` (global base context)
plus an optional existing `profile` (from `profile_store`) selected as the
"co-host persona" — the same mechanism Conversation already uses; no new
persona concept.

## API / WS contract

- `WS /v1/livehost/stream?audio_codec=pcm16|opus&audio_out=opus&output=audio,text`
  — same audio input/output contract as `/v1/conversation/stream`. Existing
  server events (`session_started`, `partial`, `final`, `audio_chunk`,
  `done`, `error`) plus two new ones:
  - `social_event` — surfaces a raw incoming comment/gift to the UI
    immediately, even before/without a reply (so an overlay can show the chat
    bubble as it happens).
  - `social_reply` — marks which turn was triggered by which social event
    (or batch), so the UI can render "replying to @user" distinctly from a
    voice-triggered reply.
- `POST /v1/livehost/{session_id}/connect {unique_id}` — attaches a
  `TikTokLiveIngestor` to an already-open livehost session.
- `POST /v1/livehost/{session_id}/disconnect`
- `GET /v1/livehost/{session_id}/status` — ingestor connection state.

## Error handling

- A failing social turn (LLM or TTS error) is logged and the event is
  dropped; it does not crash the session — this differs from a voice-turn
  error, which should surface to the streamer directly.
- If TikTok was never connected (no `connect` call), the session behaves
  exactly like plain-voice Conversation — the social feature is an optional
  add-on, not a hard requirement to use livehost at all.

## Testing

- `EventScheduler`: pure unit tests — priority ordering, the
  individual-vs-batch threshold, queue cap behavior, and the "never drop
  gifts/mentions" rule. No WS/TikTok mocking needed.
- `TikTokLiveIngestor`: unit tests against a mocked `TikTokLiveClient` —
  verify backoff growth, reset-on-event, transition to `offline_waiting` on
  room-ended, and no duplicate client creation on overlapping reconnect
  triggers.
- `LiveHostOrchestrator`: turn arbitration tests (voice preempts social while
  VAD is active) using a fake history/responder, following the existing test
  patterns for `conversation/responder.py`.
- Integration: end-to-end WS test against `/v1/livehost/stream` with mock
  LLM/TTS (`ENABLE_MOCK_ENGINES=true`, as used today) plus injected
  `SocialEvent`s to verify `social_event`/`social_reply` are emitted
  correctly.
