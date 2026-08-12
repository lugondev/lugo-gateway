# API Reference (v1)

Base URL (local): `http://localhost:8000`

All JSON responses use a common envelope:

```json
{ "success": true, "data": { ... }, "error": null }
```

On a handled domain error the envelope is `{ "success": false, "error": "<message>" }`
with an appropriate HTTP status (e.g. `400` for an unknown engine).

---

## Authentication

Every request is checked by `AuthGuardMiddleware` (`app/core/auth_guard.py`), which is
**default-deny**: a route not explicitly classified there requires at least a logged-in
user, not public access.

Identity comes from exactly one of two sources, never both for a single request:
- **Cookie session** — set by `POST /api/auth/login`. The normal path for browser
  clients (the `/ui` playground, `/static/*` pages).
- **Bearer token** — `Authorization: Bearer <token>`. If this header is present it is
  the *only* identity source considered for that request; an invalid/expired bearer
  token gets an immediate `401` and never falls back to the cookie session. A bearer
  identity always resolves to `role="user"`, even for an account that is `admin` in
  the DB — a bearer-holding client can never escalate to admin.

Both paths **re-read the user from the DB on every request**. The cookie carries only a
`user_id`; `disabled` and `role` are never trusted from the signed cookie. So disabling
an account cuts its live sessions off immediately (`401`, and the cookie is dropped), and
promoting or demoting a user takes effect on their next request rather than at their next
login.

**Public, no credentials at all:**
- `/` and `/health`
- `/static/login.html` and its assets (`/static/js/auth.js`, `/static/styles.css`,
  `/static/brand/favicon.svg`, `/static/brand/logo-mark-light.svg`)
- `/api/auth/*` (login/signup/refresh/logout — the only way to obtain a session)
- `/v1/devices/pair/init` and `/v1/devices/pair/status` (the device pairing
  handshake — the device itself has no login yet)

Public does not mean unlimited. `POST /api/auth/login` and `POST /api/auth/token` share
one rate limiter, keyed both per `(client, username)` and per client; `POST
/api/auth/signup` is limited per client. Over budget returns
`429 {"detail": "too many attempts, try again shortly"}`. Only **failed** password
attempts are charged, so nobody can lock a user out of their own account by spending
their budget. Keying on the client address needs `TRUSTED_PROXY_HOPS` set behind a
reverse proxy — see `docs/runbook.md`.

**Requires a logged-in user (any role):** everything under `/ui`, `/static/`,
`/v1/events`, `/v1/conversation`, `/v1/livehost`, `/v1/profiles`,
`/v1/mcp`, `/v1/stt`, `/v1/tts`, `/v1/sessions`, `/v1/stats`, `/v1/devices/mine`
(including the own-device subresources `POST /v1/devices/mine/{device_id}/revoke`
and `POST /v1/devices/mine/{device_id}/profile`),
`/v1/devices/pair/claim`, plus the read-only carve-outs `/v1/model_registry/options`,
`/v1/model_registry/defaults`, and `/v1/usage/me`.

**Requires `role == "admin"`:** `/v1/system`, `/v1/models`, `/v1/users`,
`/v1/devices` (other than the `mine`/`pair/claim` carve-outs above), `/v1/model_registry`
(other than the `options`/`defaults` carve-outs above), `/v1/providers`, `/v1/usage`
(other than the `me` carve-out above), `/v1/quotas`, and the documentation/introspection
surface: `/agents-docs`, `/docs`, `/redoc`, `/openapi.json`.

An unauthenticated request gets `401 {"success": false, "error": "login required"}`
(or a `307` redirect to `/static/login.html` for a browser navigation), and an
authenticated-but-wrong-role request against an admin route gets
`403 {"success": false, "error": "admin only"}`.

This is the current, ground-truth list — read `auth_guard.py`'s `_PUBLIC_PATHS`,
`_STATIC_ALLOWLIST`, `_NO_AUTH_PREFIXES`, `_USER_PREFIXES`, and `_ADMIN_PREFIXES`
tuples directly before relying on it, since prefixes can be added without this doc
being updated in lockstep.

**WebSocket routes** (`/v1/conversation/stream`, `/v1/lugo/stream`, etc.) are not
covered by the HTTP middleware above; they resolve identity separately via
`resolve_ws_identity` (same three sources: bearer subprotocol, cookie session, or a
paired-device/fleet token) and, for routes that resume a session by caller-supplied
id, enforce ownership per-connection (a caller can't attach to another user's
session by guessing its id).

---

## Health & meta

### `GET /health`
Liveness probe. → `{ "status": "ok" }`

### `GET /`
Service banner. → `{ "service": "...", "env": "dev" }`

### `GET /ui`
Browser playground (static HTML/JS at `/static`).

---

## STT — Speech to Text

### `GET /v1/stt/engines`
List configured engines and their availability.

```json
{
  "success": true,
  "data": [
    { "engine": "vosk", "mode": "local", "available": true, "configured": true, "detail": "vosk-model-small-en-us-0.15" },
    { "engine": "whisper", "mode": "local", "available": true, "configured": true, "detail": "small · cached" },
    { "engine": "whisper_service", "mode": "remote", "available": true, "configured": true, "detail": "whisper-1" }
  ]
}
```

`available` reflects whether the engine is usable now: Vosk needs its model on disk,
whisper needs faster-whisper installed, `whisper_mlx` needs mlx + a built model
(Apple Silicon), remote engines need a Model Registry entry with valid credentials.
`detail` is the specific model/version. Clients should list only `available` engines.

**Remote STT configuration:** To enable `whisper_service` or `eventlab`, create a Model Registry
entry via `POST /v1/model_registry` with `kind="stt"`, `engine="whisper_service"` (or
`"eventlab"`), and supply `base_url` and `api_key` (see Model Registry section).

### `POST /v1/stt/warm?engine=<engine>`
Preload a heavy model into memory (~10–20s the first time; cached after). The UI
calls this before the first conversation turn so it isn't a cold wait.

### `POST /v1/stt/transcribe`
Batch transcription. `multipart/form-data`:

| field | type | notes |
|-------|------|-------|
| `audio` | file | WAV PCM16 mono required for `vosk`; whisper accepts common formats |
| `engine` | string | `vosk` \| `whisper` \| `whisper_local` \| `whisper_mlx` \| `whisper_service` \| `eventlab` |
| `language` | string? | optional hint, e.g. `en`, `vi` |
| `denoise` | bool? | spectral noise reduction (default from admin System settings > preprocessing) |
| `vad` | bool? | VAD gate (default from admin System settings > preprocessing) |
| `vad_backend` | string? | `energy` \| `silero` \| `pyannote` (default from admin System settings > preprocessing) — see runbook "VAD backends" |

Preprocessing (`denoise`/`vad`) applies to mono PCM16 WAV input; other formats pass
through. `vad` also drives faster-whisper's internal `vad_filter`.

Response `data` is an `STTResult`:

```json
{ "engine": "vosk", "text": "hello world", "is_final": true, "confidence": null }
```

Errors: invalid audio / missing model → `400` with a descriptive message.

### `WS /v1/stt/stream`
Real-time transcription. Connect with query params:

```
ws://localhost:8000/v1/stt/stream?engine=vosk&language=en&sample_rate=16000&denoise=false&vad=true
```

`denoise` and `vad` toggle per-frame noise reduction / VAD gating (defaults from settings).

**Audio contract:** raw PCM signed-16, mono, at `sample_rate` (default from the
admin System tab's configured stream sample rate, 16 kHz).

Client → server:
- Binary frames: raw PCM chunks.
- Text control: `{"type":"flush"}` (emit a final for buffered audio) or
  `{"type":"end"}` (finalize, emit `done`, close).

Server → client (JSON `StreamEvent`):

| `event_type` | when | payload |
|--------------|------|---------|
| `session_started` | on connect | `session_id`, `sample_rate` |
| `partial` | interim hypothesis (Vosk only) | `STTResult` (`is_final:false`) |
| `final` | utterance/segment finalized | `STTResult` (`is_final:true`) |
| `error` | bad engine / missing model / decode error | `{ "message": "..." }` |
| `done` | stream ended | `{ "message": "stream ended" }` |

Engine behavior:
- **Vosk** decodes incrementally → real `partial` then `final` per utterance.
- **Whisper / remote** buffer all audio and return a single `final` on flush/end.

Events are also mirrored to the SSE channel `GET /v1/events/sessions/{session_id}`.

---

## Conversation (voice turn-taking)

### `WS /v1/conversation/stream`
A unified **text/audio → text/audio** gateway (browser + IoT). Input is either audio
frames (VAD-endpointed) or a text message; output is text events and/or synthesized
audio. Supports the full matrix: audio→audio, text→audio, audio→text, text→text.

```
ws://localhost:8000/v1/conversation/stream?profile=vi&sample_rate=16000&audio_codec=opus&output=audio,text&audio_out=opus&output_sample_rate=24000
```

| query param | default | meaning |
|-------------|---------|---------|
| `voice` / `language` | settings | per-session voice/language (engine selection is profile-or-server-default only, see below) |
| `profile` | — | named **chatllm profile** (see below) — sets LLM model/system prompt/TTS/MCP tools/memory in one shot |
| `sample_rate` | 16000 | input audio rate (Hz) |
| `audio_codec` | `pcm16` | **input** codec: `pcm16` or `opus` |
| `output` | `audio,text` | what to send back: any of `audio`, `text` |
| `audio_out` | `wav` | reply-audio delivery: `wav` (WAV or MP3 pushed as a binary WebSocket frame per sentence) or `opus` (binary Opus frames pushed — for devices). Unrecognized values normalize to `wav`. |
| `output_sample_rate` | 24000 | output Opus frame rate when `audio_out=opus` |
| `opus_pace` | server config | per-connection override of Opus playback pacing (see below); `0`/`false` disables it for this session only. Omit to inherit the server-wide default — this is what device firmware does. |

**`profile`** does double duty:
1. If it names a saved profile (`POST /v1/profiles`), the session uses that profile's
   `stt.engine`/`language`, `llm` (base_url/api_key/model), `system_prompt`,
   `tts.engine`/`tts.voice`, `mcp_servers`, and `memory` settings — overriding server
   defaults. There is no per-request engine override query param — STT/TTS engine
   selection is always profile config, else the server-wide `default_stt_engine`/
   `default_tts_engine` (see `GET /v1/system/config`'s `engines` group).
2. If it matches a built-in **language preset** (`vi` / `en` / `multi` / `en_vi`), it also
   selects the STT engine + language for that language, unless `language` is passed
   explicitly. A profile can be named e.g. `vi` to get both behaviors at once.

If `profile` is set but unknown, the server replies with a `warning` event and falls back
to defaults (the connection still proceeds).

Client → server:
- binary frames — audio input (PCM16, or Opus packets when `audio_codec=opus`).
- `{"type":"text","text":"…"}` — a text-input turn (no mic).
- `{"type":"abort"}` cancel turn · `{"type":"end"}` finalize+close.
- `{"type":"new_session"}` — end this conversation and start a fresh one on the same
  socket; the server answers `{"event":"session_rotated","session_id":…,"previous_session_id":…}`
  (`{"type":"session_new", …}` on the Lugo protocol). The old session is marked ended
  and its memories extracted, exactly as if the socket had closed. A turn already in
  flight **finishes first** and the rotation happens right after it; send
  `{"type":"abort"}` beforehand to cut that turn short instead.
- `{"type":"reset"}` — clear the in-memory context only. It keeps writing to the SAME
  stored session, so a reset does not produce a separate History entry and does not
  trigger memory extraction. Left unchanged for compatibility; new clients should use
  `new_session`.

**Input audio** (`audio_codec`): `pcm16` (raw 16-bit mono) or `opus` (raw packets, ~10×
less bandwidth — native for ESP32/RPi firmware + browser WebCodecs; server decodes via
libopus, falls back to `pcm16` if absent).

**Output audio** (`audio_out=opus`): each reply sentence is sent as JSON `audio_start`
`{turn, chunk_index, text?, codec:"opus", sample_rate, frames}`, then `frames` binary
Opus packets (mono @ `output_sample_rate`, 60 ms each), then `audio_end` `{turn,
chunk_index}`. By default the packets are **paced**: the first ~5 go out immediately
(fast first audio), the rest at one 60 ms frame apart, sized so a small embedded ring
buffer (ESP32/RPi) isn't flooded on long replies. Browsers don't have that constraint
and can hold seconds of audio
queued in `AudioContext`, so the web client sends `opus_pace=0` to receive each
sentence's packets back-to-back as soon as they're encoded — the browser's own
scheduling becomes the jitter buffer, which tolerates far more network/main-thread
jitter than the ~300ms server-side cushion. See
`docs/superpowers/specs/2026-07-28-web-audio-jitter-buffer-design.md` for the full
rationale. Browsers can decode the Opus frames via WebCodecs `AudioDecoder` — see
`docs/device-integration.md` §6.

**Output audio** (`audio_out=wav`, the default): each reply sentence is sent as
JSON `audio_start` `{turn, chunk_index, text?, codec:"wav"|"mp3"}`, then **one**
binary WebSocket frame carrying the complete audio container (the whole WAV or
MP3 file for that sentence, not a stream of packets), then `audio_end` `{turn,
chunk_index}`. `codec` mirrors the TTS provider's media type: `audio/wav` →
`"wav"`, `audio/mpeg` (edge_tts) → `"mp3"`. Nothing is written to disk — there is
no artifact directory in this path and no URL is ever sent.

Server → client events (`{"event": ...}`):

| `event` | when | key fields |
|---------|------|-----------|
| `session_started` | on connect | `stt_engine`, `stt_detail`, `tts_engine`, `tts_detail`, `responder`, `llm_model`, `audio_codec`, `output`, `audio_out`, `output_sample_rate` |
| `speech_start` | user starts speaking | — |
| `speech_end` | VAD detects end of turn | `speech_ms` |
| `processing` | transcribing + generating | `turn` |
| `user_transcript` | STT result (or echoed text input) for the turn | `text` |
| `response_text` | assistant reply text (when `output` includes `text`) | `text`, `responder` |
| `audio_start` / `audio_end` | brackets one sentence's binary audio frame(s) (when `output` includes `audio`) | `turn`, `chunk_index`, `text?` (start only), `codec:"wav"\|"mp3"` (default `audio_out=wav`) or `codec:"opus"`, `sample_rate`, `frames` (`audio_out=opus`) |
| `aborted` | turn cancelled (barge-in / superseded) | `reason` |
| `turn_done` | turn complete | `turn` |
| `error` / `done` / `reset` | — | — |

Turn-taking uses an energy VAD endpointer (`CONVERSATION_*` settings). Long replies
are sentence-split and synthesized chunk-by-chunk so playback starts early. A
`speech_start` mid-reply is barge-in: the in-progress turn is cancelled (`aborted`).

The reply comes from:
- **Echo** — built-in, when no LLM is configured.
- **Text LLM** (cascade) — any OpenAI-compatible chat endpoint (local Ollama or an
  online provider). `responder` = `"llm"`, `llm_model` = the active model.

### Conversation LLM config

| route | does |
|-------|------|
| `GET /v1/conversation/llm` | current config: `base_url`, `model`, `api_key_set`, `responder` |
| `POST /v1/conversation/llm` | set `{base_url, api_key, model}` at runtime (any OpenAI-compatible endpoint). API key kept in memory only — never echoed or persisted |
| `POST /v1/conversation/llm/reset` | revert to the `.env` config |
| `POST /v1/conversation/chat` | `{messages:[…]}` → text reply from the active responder. Accepts the same `?profile=` and `?session_id=` params as the WS stream |

### Profiles — named chatllm presets

A **profile** bundles everything a conversation session needs into one name: LLM
endpoint/model, system prompt, TTS engine/voice, MCP tool servers, and memory settings.
Activate one on any conversation session with `?profile=<name>` (WS stream or
`POST /v1/conversation/chat`) — see `profile` in the WS query-param table above and
[device-integration.md](device-integration.md) for ESP32/RPi usage.

| route | does |
|-------|------|
| `GET /v1/profiles` | list all profiles (`api_key` masked as `***`) |
| `POST /v1/profiles` | create/replace a profile — body: `{name, nickname, llm:{base_url,api_key,model}, system_prompt, tts:{engine,voice}, mcp_servers:[…], memory:{enabled,mode,top_k,extractor_model,embed_model}}` |
| `GET /v1/profiles/{name}` | fetch one profile |
| `PUT /v1/profiles/{name}` | update a profile (full replace) |
| `DELETE /v1/profiles/{name}` | delete a profile |

Example — create a profile for a hands-free kitchen device pointed at a local Ollama model:
```bash
curl -X POST http://localhost:8000/v1/profiles \
  -H "Content-Type: application/json" \
  -d '{
        "name": "kitchen",
        "llm": {"base_url": "http://localhost:11434/v1", "model": "qwen2.5:7b"},
        "system_prompt": "You are a concise kitchen assistant.",
        "tts": {"engine": "vieneu"}
      }'
```
Then point the device at `?profile=kitchen` instead of setting TTS engine/LLM config
per-request.

### `WS /v1/lugo/stream`

The **Lugo** device protocol — a connect-on-wake WebSocket for ESP32/RPi-class voice
devices (and future agent/browser clients). Unlike `/v1/conversation/stream`
(always-streaming, query-param config), a Lugo device stays disconnected while idle,
opens the socket only on a wake trigger, identifies itself with a **profile name**
instead of raw engine/voice choices, and the **server** owns the idle-disconnect
timer.

```
ws://localhost:8000/v1/lugo/stream
```

**Lifecycle:** SLEEP (no WS, radio idle) → wake trigger (button / local command —
on-device **wake word is Phase 2**) → open WS → client sends `wakeup` → server
resolves the profile and replies `welcome` → LISTENING (streaming mic, server VAD
endpoints the turn) ⇄ SPEAKING (server pushes `tts` + audio) → after
`idle_timeout_s` of inactivity the **server** sends `goodbye` and closes the socket
→ device returns to SLEEP.

**Handshake** — the first frame must be a `wakeup` text frame:
```json
{"type": "wakeup", "profile": "kitchen", "audio_params": {"format": "opus", "sample_rate": 16000, "frame_duration": 60}}
```
The server resolves LLM / TTS / system prompt / MCP tools / memory from the named
profile — the device never sends raw engine/voice choices (STT still comes from
server defaults, not the profile; see "Profiles" above) — and replies:
```json
{"type": "welcome", "session_id": "…", "transport": "websocket", "audio_params": {"sample_rate": 24000}, "idle_timeout_s": 30}
```
`idle_timeout_s` echoes the profile's `session.idle_timeout_s` (default 30; `0` =
never auto-disconnect), so the device arms its watchdog from server truth instead
of a hardcoded value. If `profile` is set but unknown, the server replies
`{"type":"error","message":"profile '<name>' not found"}` and closes the socket.
If the device is *paired* (connected with its own `device_token`) and has no
profile bound to it server-side, the server replies
`{"type":"error","message":"this device is not assigned to a profile; assign one
in the admin console"}` followed by an ordinary close (not a 401/403/4401-style
close) — a `wakeup` always resolves or fails loudly, never a silent fallback. Any
other message, or a binary frame, as the first frame is also an `error` + close.

**Binary framing (v3):** audio travels on WebSocket binary frames wrapped in a
4-byte header — `struct { uint8 type; uint8 reserved; uint16 payload_size
(big-endian); } + payload`. `type` 0 = Opus audio; `type` 1 = JSON is reserved for
a future JSON-over-binary path (Phase 1 sends all JSON control on **text** frames
only). Reply audio (server → client) is always v3-wrapped; Opus packets from the
device on the way up are decoded directly and don't require the v3 header.

Client → server:

| `type` | payload | meaning |
|--------|---------|---------|
| `wakeup` | `{profile, audio_params:{format,sample_rate,frame_duration}}` | handshake (first frame only) |
| `text` | `{text}` | text-input turn (no mic) |
| `abort` | `{reason}` | **barge-in** — cancel the bot's in-flight turn; the connection stays open |
| `listen` | `{state, mode}` | turn/listen control; Phase 1 no-op — server VAD drives turn segmentation in `auto` mode |
| `new_session` | *(none)* | end this conversation and start a fresh one **without dropping the socket**; answered with `session_new`. A turn in flight finishes first — send `abort` first to cut it short |
| *(binary)* | Opus packets | mic audio up (v3 wrapping optional on uplink) |

Server → client:

| `type` | payload | when |
|--------|---------|------|
| `welcome` | `{session_id, transport, audio_params, idle_timeout_s}` | reply to a valid `wakeup` |
| `stt` | `{text, final}` | transcription result for the turn |
| `tts` | `{state:"start"\|"sentence_start"\|"stop", text?}` | brackets the reply; `sentence_start` carries the sentence text as it's synthesized |
| `mcp` | `{...}` | tool/command output |
| `error` | `{message}` | handshake failure or mid-session error |
| `session_new` | `{session_id, previous_session_id}` | reply to `new_session` — the old conversation is ended and its memories extracted; **persist the new id**, or a reconnect resumes the conversation you just left |
| `goodbye` | `{reason:"idle_timeout"}` | server-initiated idle disconnect; the socket closes right after |
| *(binary)* | v3 `type=0` Opus packets | reply audio down |

**Barge-in:** sending `abort` while the bot is speaking cancels the in-flight turn
(stops the `tts`/audio stream) without dropping the connection — the device can
immediately start a new turn (`text` or mic audio). `abort` with no active turn is
a safe no-op.

**New session:** `new_session` ends the current conversation (marks it ended and runs
memory extraction on it, exactly as a disconnect would) and opens a fresh one under the
same profile and owner, then replies `session_new`. The audio pipeline is untouched — no
re-handshake, no engine reload. It is a no-op that returns the same id when the current
conversation has no turns yet, so pressing a "start over" button twice cannot litter the
history with empty rows. This matters most for a mains-powered device: it never
disconnects, so without `new_session` its whole life is one conversation and memory
extraction (which only runs when a conversation ends) never runs at all.

A turn in flight is **not** cancelled: the request is parked and the rotation happens
when that turn ends. This is what makes a voice-driven "start over" work — the device's
`self.session.new` tool asks for it from *inside* a turn, and that turn has to be allowed
to finish confirming it (the reply belongs to, and is stored under, the conversation being
left). It also means the tool result must be written to the socket **before**
`new_session`. A client that means "stop talking and start over now" — a button, not a
voice request — sends `abort` first; with no turn left in flight the rotation is
immediate.

**Idle timeout:** the server tracks last activity (speech, a turn, or audio
playing) and, once `idle_timeout_s` elapses with the connection otherwise idle,
sends `goodbye{reason:"idle_timeout"}` and closes the WebSocket. Setting a
profile's `session.idle_timeout_s` to `0` disables this (the connection is only
closed by the client or a transport drop).

**Spoken announcements:** two server-initiated moments say so out loud, in the
profile's own voice, with the tail of the conversation as context — the line is
written by the profile's LLM per event, not stored anywhere:

- after a rotation nobody has spoken for (a button or a bare `new_session`; the
  voice-tool path is skipped because the turn that asked already confirmed it), and
- just before an idle `goodbye`.

Both arrive as an ordinary speaking turn (`tts` start → audio → stop) and are stored
in the conversation they belong to — the fresh one for a rotation, the one being
closed for a goodbye. Neither happens when nothing was ever said: an empty
conversation has no fresh start to announce and no goodbye to say. If the LLM or the
TTS engine fails, the server stays silent and sends `error` naming the stage, so a
device can show *why* it said nothing.

**Not yet implemented (Phase 2):** on-device wake-word detection (the `wakeup`
trigger is button/local-command only in Phase 1), a live `listen{detect}` mode,
and remote-call (server-initiated wake over an always-on channel). The `listen`
message and `wakeup` shape already reserve room for these.

---

## TTS — Text to Speech

### `GET /v1/tts/engines`
List configured TTS engines.

Returns engines with `available`, `detail` (model/version), `mock`, `default` fields.
Available engines: `omnivoice` (24 kHz, multilingual, subprocess-based), `vieneu` (VieNeu-TTS v3 turbo,
48 kHz, Vietnamese), and others defined via Model Registry.

**OmniVoice configuration:** To use OmniVoice, create a Model Registry entry via `POST /v1/model_registry`
with `kind="tts"`, `engine="omnivoice"`, and optional engine-specific config in the `config` dict
(e.g. `device`, `dtype`). See Model Registry section for details.

### `GET /v1/tts/voices?engine=vieneu`
Lists VieNeu preset voices `[{ "label", "voice" }]`.

### `POST /v1/tts/synthesize`
Batch synthesis. JSON body (`TTSRequest`):

```json
{
  "text": "Hello world",
  "engine": "omnivoice",
  "language": null,
  "speed": null,
  "instruct": null,
  "ref_audio_path": null,
  "ref_text": null
}
```

Voice modes (OmniVoice):
- **Clone** — provide `ref_audio_path` (+ optional `ref_text`).
- **Design** — provide `instruct`, e.g. `"female, low pitch, british accent"`.
- **Auto** — provide neither.

**Response: the audio itself**, not a JSON body pointing at a file. `200 OK` with:
- Body: the raw encoded audio (`audio/wav` for every engine except `edge_tts`, which
  is `audio/mpeg`).
- Headers: `X-TTS-Engine`, `X-TTS-Sample-Rate`, `X-TTS-Process-Seconds`, and
  `X-TTS-Duration-Seconds` (WAV only — an MP3 duration would have to be
  estimated, so it's omitted rather than guessed). These are listed in CORS
  `expose_headers` so cross-origin clients (e.g. lugo-web-client) can read them.

This used to write a temporary WAV under `artifacts/` and return a URL pointing
at it — that indirection existed only because JSON can't carry binary. Nothing
persisted that reference (no message ever pointed back at it after the initial
response), so the file was pure churn and an unauthenticated-by-default
surface; returning bytes removes both. The pseudo-streaming SSE job route pair
that used to exist alongside this one for multi-segment playback (a
job-starting POST plus a per-job SSE subscription) has been deleted for the
same reason — the conversation socket's `audio_start`/binary-frame/`audio_end`
framing (see above) now covers streamed, sentence-by-sentence playback without
ever handing out a file reference.

A failed synthesis returns a JSON error response (502) instead of a placeholder.

---

## Events (SSE)

### `GET /v1/events/sessions/{session_id}`

**Requires a logged-in user, and ownership.** A non-admin caller who isn't the
owner of the session gets `404`, not `403` — indistinguishable from the id
simply not existing, so a caller can't use the response to fish for which ids
are valid. Admins (and dev mode with auth disabled) are unscoped.

The session row must already exist. If it doesn't (e.g. the id hasn't been
created by the producer — the STT WebSocket, see `WS /v1/stt/stream` above —
yet), the request 404s immediately rather than waiting. **This means
subscribing *before* the producer creates the session fails outright** — the
SSE client must wait until the session exists (e.g. until the WS side has
signaled it) before calling this endpoint; it cannot preemptively subscribe
and rely on buffered replay to catch the earliest events.

Server-Sent Events stream. Each message is `event: <type>` + `data: <StreamEvent JSON>`.

The bus **buffers** events per channel, so once subscribed, connecting slightly
after the producer started still replays earlier events on that channel (e.g.
`session_started`) — this buffering is about timing *after* the channel exists,
not a way around the "session must already exist" requirement above. The
stream **closes itself** after a terminal `done` event.

The events mirrored here are the same ones sent over the STT WebSocket —
`session_started`, `partial`, `final`, `error`, `done` — see the table under
`WS /v1/stt/stream` above.

---

## System & Models

### `GET /v1/system/status`
Aggregated runtime status: app env, STT engines (+ remote `configured`), TTS mock flag
and OmniVoice presence, whisper-local cache state, active Vosk model + installed Vosk
models, and voice-clone reference-clip count/size (the `artifacts` field — synthesized
reply audio is never persisted, so this counts only uploaded reference-audio clips, not
generated audio; see "Artifacts" below).

### `GET /v1/system/config`
Fetch the system configuration (preprocessing, conversation tuning, engine defaults).

Response `data`:
```json
{
  "base_context": "...",
  "engines": {
    "default_stt_engine": "vosk",
    "default_tts_engine": "omnivoice",
    "default_tts_engine_voice": "",
    "stt_segment_long_enabled": false,
    "stt_segment_min_seconds": 30.0,
    "stt_segment_concurrency": 4
  },
  "conversation": { ... },
  "preprocessing": { ... }
}
```

Key changes from earlier API versions:
- **Remote STT config** (`whisper_service`, `eventlab`) is no longer stored in SystemConfig.
  Configure remote STT engines via `POST /v1/model_registry` with `kind="stt"` entries (see below).
- **OmniVoice TTS config** is no longer stored in SystemConfig. Configure OmniVoice via
  `POST /v1/model_registry` with `kind="tts"` entries and store engine-specific settings in the
  `config` dict.
- **stt_local per-engine fields** have been removed — device/compute_type first, then the
  default model / model path and whisper decode tuning (`vosk_model_path`,
  `whisper_local_model`, `whisper_vad_filter`, `whisper_beam_size`,
  `whisper_condition_on_previous_text`, `whisper_initial_prompt`,
  `whisper_mlx_model_path`, `qwen3_asr_model`). Configure them per engine via the
  Model Registry `model_id=""` sentinel entries (`kind="stt"`, `engine="whisper_local"` /
  `"whisper_mlx"` / `"qwen3_asr"` / `"vosk"`), stored in the `config` dict — e.g.
  `{"default_model": "large-v3-turbo", "vad_filter": true, "beam_size": 1,
  "condition_on_previous_text": false, "initial_prompt": "", "device": "cpu",
  "compute_type": "int8"}` for `whisper_local`, `{"model_path": "..."}` for
  `vosk`/`whisper_mlx`.
- **The `stt_local` group is gone entirely.** Its 3 engine-agnostic long-audio
  segmentation fields (`stt_segment_long_enabled`, `stt_segment_min_seconds`,
  `stt_segment_concurrency`) moved into `engines`, above. Its 4 remaining fields
  (`stt_model_dir`, `vosk_model_base_url`, `stt_stream_sample_rate`,
  `stt_glossary_path`) are deployment-time constants now — set via env vars
  (`STT_MODEL_DIR`, `VOSK_MODEL_BASE_URL`, `STT_STREAM_SAMPLE_RATE`,
  `STT_GLOSSARY_PATH`), not exposed via this endpoint.
- **`preprocessing.pyannote_vad_model`/`pyannote_auth_token` are gone too** — same
  reasoning, now `PYANNOTE_VAD_MODEL`/`PYANNOTE_AUTH_TOKEN` env vars.
- **No per-request `?stt_engine=`/`?tts_engine=` query param** on `/v1/conversation/stream`,
  `/v1/livehost/stream`, or the Lugo protocol — engine selection is profile config, else
  `engines.default_stt_engine`/`default_tts_engine`.

### `PUT /v1/system/config`
Update the system configuration. Send a partial or full body; absent fields retain their current
values. `SystemConfig` currently has no secret fields (`pyannote_auth_token`, the last one, moved to
the `PYANNOTE_AUTH_TOKEN` env var).

---

## Model Registry

The **Model Registry** stores engine configurations for STT, TTS, and LLM providers. Each entry
binds an engine to credentials, connection details, and engine-specific parameters.

### `GET /v1/model_registry`
List all model registry entries.

```json
{
  "success": true,
  "data": [
    {
      "id": "whisper_service_prod",
      "kind": "stt",
      "engine": "whisper_service",
      "model_id": "whisper-1",
      "label": "Whisper API (prod)",
      "stage": "stable",
      "enabled": true,
      "base_url": "https://api.openai.com/v1",
      "api_key": "sk-...abc",
      "config": { }
    },
    {
      "id": "qwen3_asr_local",
      "kind": "stt",
      "engine": "qwen3_asr",
      "model_id": "Qwen/Qwen3-ASR-0.6B",
      "label": "Qwen3 ASR (0.6B)",
      "stage": "stable",
      "enabled": true,
      "base_url": "",
      "api_key": "",
      "config": {
        "device": "mps",
        "compute_type": "float16"
      }
    },
    {
      "id": "omnivoice_standard",
      "kind": "tts",
      "engine": "omnivoice",
      "model_id": "k2-fsa/OmniVoice",
      "label": "OmniVoice",
      "stage": "stable",
      "enabled": true,
      "base_url": "",
      "api_key": "",
      "config": {
        "device": "mps",
        "dtype": "float16"
      }
    }
  ]
}
```

Fields:
- `id` — unique identifier for the entry
- `kind` — `"stt"`, `"tts"`, or `"llm"`
- `engine` — provider name, e.g. `whisper_service`, `eventlab`, `qwen3_asr`, `whisper_local`, `omnivoice`, `vieneu`, `openai`
- `model_id` — model identifier (HF repo, OpenAI model name, etc.)
- `label` — human-readable label for the UI
- `stage` — `"stable"` or `"experimental"`
- `enabled` — whether the entry is active
- `base_url` — for remote (STT/LLM) providers; OpenAI-compatible base URL
- `api_key` — masked on read (e.g. `sk-...abc`); updated only if non-blank
- `config` — engine-specific parameters dict (device, compute_type, dtype, timeout, etc.)

### `POST /v1/model_registry`
Create a new model registry entry.

Request body:
```json
{
  "kind": "stt",
  "engine": "whisper_service",
  "model_id": "whisper-1",
  "label": "Whisper API",
  "stage": "stable",
  "base_url": "https://api.openai.com/v1",
  "api_key": "sk-...",
  "config": {}
}
```

The endpoint validates the configuration by making a test call to the provider (e.g. transcribing
a silent WAV for STT, synthesizing sample text for TTS, or querying a chat endpoint for LLM).
Optional `sample_text` (default `"xin chào"`) customizes the validation text; used for TTS synthesis and LLM chat test calls.
If validation fails → `400` with the provider's error detail.

On success, returns the created entry with a masked `api_key`.

### `PATCH /v1/model_registry/{id}`
Update a model registry entry (partial update).

Request body:
```json
{
  "enabled": false,
  "stage": "experimental",
  "config": { "device": "cuda" }
}
```

Fields to update:
- `enabled` — toggle entry on/off
- `stage` — change to `"stable"` or `"experimental"`
- `base_url` — update endpoint URL (for remote providers)
- `api_key` — update credentials; blank or absent means "keep existing"
- `config` — replaces the entire config dict (not a merge) — submit the full desired config, not just the changed keys

If the entry is not found → `404`. On success, returns the updated entry with a masked `api_key`.

**Side effects:** Updating certain entries triggers runtime reinitialization:
- `kind="stt"` with `engine="whisper_service"` or `engine="eventlab"` → reinit remote STT providers
- `kind="stt"` with `engine="qwen3_asr"` and `config` changed → clear model cache
- `kind="tts"` with `engine="omnivoice"` → reset OmniVoice subprocess

---

### `GET /v1/models`
Vosk and Whisper model catalogs and state:

```json
{
  "vosk": {
    "installed": [{ "name": "vosk-model-small-en-us-0.15", "size_bytes": 70898967, "path": "..." }],
    "suggestions": [{ "name": "vosk-model-small-vn-0.4", "label": "Vietnamese (small)", "installed": false }],
    "jobs": { "vosk-model-small-vn-0.4": { "state": "downloading", "progress": 0.42, "error": null } },
    "base_dir": "models/stt"
  },
  "whisper": {
    "active": "small",
    "models": [
      { "size": "small", "label": "Small (default)", "cached": true, "active": true, "size_bytes": 503000000, "job": null }
    ]
  }
}
```

Vosk `jobs[name].state` is `downloading` \| `installed` \| `error` (with `progress` 0–1).
Whisper `job.state` is `downloading` \| `installed` \| `error` (progress is indeterminate).
Poll this endpoint while a download is active.

### Vosk
- `POST /v1/models/vosk/download` — body `{ "name": "vosk-model-small-vn-0.4" }`. Downloads
  `{vosk_model_base_url}/{name}.zip` in the background, extracts into the configured
  STT model dir (admin System tab > stt_local group). Invalid names → `400`; a missing
  model surfaces as a job `error` (HTTP 404).
- `DELETE /v1/models/vosk/{name}` — removes an installed model dir (traversal-protected);
  not installed → `400`.

### Whisper (faster-whisper)
- `POST /v1/models/whisper/download` — body `{ "size": "tiny" }`. Warms the size (fetches
  weights into the Hugging Face cache) in the background.
- `DELETE /v1/models/whisper/{size}` — removes the size's hub cache dir; not cached → `400`.
- `POST /v1/models/whisper/select` — body `{ "size": "medium" }`. Switches the active
  local-whisper model at runtime (not persisted across restarts).

> Browse the full Vosk catalog at <https://alphacephei.com/vosk/models>; any name can be
> downloaded. Whisper sizes: `tiny`, `base`, `small`, `medium`, `large-v3`.

---

## Artifacts

There is no longer an HTTP-served artifacts route. Synthesized reply audio is
never written to disk — it goes out as response bytes (`POST
/v1/tts/synthesize`) or as binary WebSocket frames (the conversation socket,
above). The `artifacts/` directory still exists on the local filesystem, but
only for voice-clone **reference audio** (`POST /v1/tts/reference-audio`,
`ref_audio_path`) — it is never mounted or served over HTTP, so there is no
`GET /artifacts/{file}` route to hit.

---

## StreamEvent schema

```json
{
  "event_type": "partial",
  "session_id": "…",
  "job_id": null,
  "sequence": 2,
  "timestamp": "2026-06-25T15:10:57.499171Z",
  "payload": { }
}
```

`timestamp` is timezone-aware UTC (ISO 8601). `sequence` is monotonic per stream.
