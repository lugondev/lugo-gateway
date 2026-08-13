# Architecture

## Overview

A single FastAPI gateway does three jobs at once, and it helps to keep them
separate in your head:

1. **A media engine** — STT and TTS behind provider interfaces, so engines swap
   without touching routes.
2. **A voice loop** — VAD endpointing → STT → LLM → per-sentence TTS, with
   barge-in, spoken over a WebSocket to browsers and to hardware.
3. **A small multi-tenant platform** — users and roles, paired devices, per-profile
   config, a unified model registry, spend metering and quotas, and an extension
   point for out-of-process plugins.

Most of the file below is about (3), because that is where the surprise usually is:
the original service was only (1) and (2), and the platform grew around it.

```
   browser (/ui)   web SPA      RPi / ESP32      plugin (livehost-api)
        │            │               │                    │
        │ cookie     │ bearer        │ device token       │ ticket + secret
        ▼            ▼               ▼                    ▼
 ┌──────────────────────────────────────────────────────────────────────────┐
 │ CORS → UploadSizeLimit → Session → AuthGuard (default-deny) → routes      │
 ├──────────────────────────────────────────────────────────────────────────┤
 │  media          voice loop          platform                             │
 │  ───────        ──────────          ────────                             │
 │  STTService     ConversationSession  auth/users/devices  model_registry   │
 │  TTSService      ├ endpointer (VAD)  profiles + memory   providers        │
 │   │              ├ responder (LLM)   mcp (tool calls)    usage + quotas   │
 │   │              ├ segmenter         sessions/history    plugins          │
 │   ▼              └ per-sentence TTS                                       │
 │  providers ──────────────────────────────────────────────┐               │
 │  local: vosk whisper whisper_mlx qwen3_asr …  remote: http_stt qwencloud… │
 ├──────────────────────────────────────────────────────────────────────────┤
 │  SQLite (aiosqlite async + sqlite3 sync for config) · settings.database_url│
 └──────────────────────────────────────────────────────────────────────────┘
```

## Request pipeline

Middleware order is load-bearing and is registered in **reverse** of the request
chain (Starlette inserts at index 0). The chain is:

```
CORS → UploadSizeLimit → Session → AuthGuard → routes
```

- **CORS outermost** so it also applies to AuthGuard's 401/403 and
  UploadSizeLimit's 413 — otherwise a cross-origin client sees an opaque network
  failure instead of a status it can react to.
- **Session before AuthGuard**, because AuthGuard reads `request.session`.
- `main.py` carries the full reasoning in comments, and
  `tests/integration/test_cors_ordering.py` pins it against real HTTP responses
  rather than list order.

### AuthGuard is default-deny

`core/auth_guard.py` classifies every path into one of four buckets —
`_NO_AUTH_PREFIXES`, `_USER_PREFIXES`, `_USER_EXACT`, `_ADMIN_PREFIXES` — and
refuses anything unclassified. Two details are deliberate:

- Prefixes match on a **segment boundary**, not bare `startswith`, so a future
  `/api/authz/...` mount cannot inherit `/api/auth`'s public status.
- User carve-outs that sit *inside* an admin prefix are matched **exactly and by
  method**. `GET /v1/model_registry/options` is a user dropdown feed, but
  `PATCH` on that same string dispatches to the admin `update_entry(entry_id="options")`
  handler — a prefix-shaped carve-out would smuggle a non-admin into it.

Adding a router therefore means classifying it;
`tests/unit/http/test_auth_guard_route_coverage.py` fails until you do.

### Identity schemes

Four, with no fallback between them:

| Caller | Credential | Resolves to |
|---|---|---|
| Admin console `/ui` | session cookie | `admin` or `user` |
| Web SPA / API clients | bearer token | always `user` — a token cannot escalate |
| RPi / ESP32 | per-device token from pairing (or `DEVICE_AUTH_TOKEN` stopgap) | the device's owner |
| Plugin | 60s ticket, resolved via `POST /api/auth/introspect` with the plugin's secret | the ticket's user |

## Components

### API layer (`app/api/routes`)

24 routers. Routes are thin: validate, resolve a service, translate domain errors.
They never embed model logic. See AGENTS.md for the full router → group map.

Two conventions hold everywhere:

- REST returns `{"success": true, "data": …}`; errors go through the global
  `AppError` handler as `{"success": false, "error": …}`. Never a plain-text 500 —
  clients parse JSON.
- **Route module globals are test seams.** Tests monkeypatch store lookups by
  their name in the route module, so moving a store import out of a route module
  breaks tests that are not obviously related to it.

### Media services

- **STTService / TTSService** — registries mapping an engine name to a provider.
  Unknown names raise `EngineNotFoundError` (→ HTTP 400 / WS `error`). Both expose
  `check_engine()` with a three-state contract: `ok` / `not_ready` (model still
  loading) / `unavailable` (dependency or model missing).
- **STT providers** implement `transcribe_bytes()` and optionally `open_stream()`
  (native streaming), `warm()`, `detail()`. 11 engines are registered — local
  (`vosk`, `whisper`, `whisper_mlx`, `qwen3_asr`, `qwen3_asr_gguf`) and remote
  (`whisper_service`, `eventlab`, `qwen3_asr_or`, `whisper_or`, `qwencloud`,
  `http_stt`). Heavy/optional engines must report `available=False` when their
  dependency is missing rather than failing at call time; MLX engines auto-hide
  off Apple Silicon so callers fall back to `whisper`.
- **TTS providers** implement `synthesize()`, and describe themselves through
  `list_voices()` / `supports_voice_clone()` — that self-description is what lets
  the console pick voices and offer cloning through a *remote* engine it knows
  nothing else about.
- **ArtifactStore** persists voice-clone **reference audio** only. Synthesized
  reply audio is never written to disk; providers return bytes straight to the
  caller. `render_audio` is the single seam where audio becomes bytes.
- **segmenter** splits reply text into sentence-sized chunks for streaming TTS.

### Boot sequence (`lifespan` in `main.py`)

Order matters and is commented in place:

1. `init_db()`, then bootstrap the first admin if the user table is empty.
2. `init_config_tables()` and touch each config store (profiles, TTS profiles,
   system config, MCP servers). MCP presets are seeded **here**, not at import
   time — importing happens during test collection too, and seeding there would
   write into whatever DB was configured at that moment (in practice, the real
   `data/app.db`).
3. Registry migrations, in a fixed order: rename dead engine names first
   (`openai_stt`/`openai_tts` → `http_stt`/`http_tts`) so everything downstream
   sees corrected names, then the per-feature migrations, then the seeder, then
   drop stale shims, then back-fill usage model ids.
4. Warm every engine any profile could select — not just the defaults — so a
   device connecting with any profile never pays a cold model load on its first
   turn. Capped by `warmup_startup_timeout_s`; on timeout the app serves cold.

On shutdown it drains background tasks (memory extraction runs at session
teardown), closes the MCP pool, and disposes the DB engine.

**Migrations only run on deploy.** A data-shape fix merged to `main` does not heal
production until the process restarts there.

### Persistence

SQLite by default, `settings.database_url` is the seam for PostgreSQL. Two access
paths coexist on purpose:

- **async engine (aiosqlite)** for request-path data: users, devices, sessions,
  memories, usage rows.
- **sync engine** (`db/sync_engine.py` — same database, same URL, sync driver) for
  the config stores: profiles, TTS profiles, MCP servers, system config. They keep
  a synchronous API because they are read from non-async call sites, including at
  import and boot. `sync_database_url()` maps the async URL to its sync twin
  (`sqlite+aiosqlite` → `sqlite`, `postgresql+asyncpg` → `postgresql+psycopg`), so
  a PostgreSQL move only has to get that mapping right.

aiosqlite connections are never pooled — each is bound to an event loop, and
pooling them across pytest's per-test loops corrupts state.

> `config_profiles` stores an LLM `api_key` inline. Never `SELECT *` that table
> into a log, a transcript, or an error message.

### Streaming (`app/streaming/event_bus.py`)

`InMemoryEventBus` gives pub/sub two properties that matter:

- **Replay** — bounded per-channel history is replayed to late subscribers, so a
  subscriber never misses `session_started` to the subscribe-after-publish race.
- **Terminal close** — `done` closes the channel and wakes subscribers with a
  sentinel, so the SSE generator stops and memory is reclaimed.

Only STT streaming publishes to this bus (`session:{id}` channels). Conversation
audio goes straight to the WebSocket and has no event-bus path.

> **Status: no consumers.** `GET /v1/events/sessions/{id}` is the only reader, and
> no first-party client subscribes to it — not the admin console, the web SPA, the
> RPi client, or the ESP32 firmware. It is live, authenticated, and unused. Decide
> to wire it or remove it rather than leaving it in this state.

### Platform services

- **Model Registry** (`services/model_registry`) — one table for STT, TTS and LLM
  entries. The active conversation LLM *is* the enabled `kind="llm"` row; a profile
  can override it. Registry rows also gate model access by stage (e.g. `testing`
  requires `can_use_testing` on the user).
- **Profiles** — per-profile LLM, STT/TTS selection, system prompt, MCP servers,
  memory config, session timeouts. `GET /v1/profiles/{name}/health` pre-flights the
  profile's STT/TTS; the same check runs internally on WS connect, which is where
  it actually gates a session.
- **Memory** (`services/memory`) — extractor, embedder, retriever, store,
  compactor. Keyed by `(user_id, profile_id)`; `""` is the shared-device bucket, so
  devices on a shared template profile don't read each other's facts.
- **Usage & quotas** — every paid call site records a usage row with model
  attribution. `tests/unit/test_every_paid_entry_point_meters.py` derives the call
  sites rather than listing them, so a new metered path can't be silently omitted.
- **Plugins** — a registry of out-of-process services (name, url, secret, mounts).
  The console injects a nav item per enabled plugin at load time; nothing about a
  specific plugin is hardcoded in `index.html`.

## Data flows

### STT streaming (WebSocket)

1. Client connects to `/v1/stt/stream` and streams raw PCM16 mono frames.
2. The route opens a per-connection `STTStream` from the provider.
3. Vosk emits `partial`/`final` as audio arrives; buffering engines emit one
   `final` on `flush`/`end`.
4. Events go to the socket and are mirrored to `session:{id}` on the event bus.

### Conversation reply audio (WebSocket, binary frames)

1. The session segments reply text into sentences and synthesizes each one.
2. Per sentence: `audio_start` (JSON: `turn`, `chunk_index`, `text?`, `codec`),
   then **one binary frame** with the complete audio container, then `audio_end`.
3. `codec` is `"wav"`/`"mp3"` (default) or `"opus"` (`?audio_out=opus`, for
   ESP32/RPi — many small Opus packets instead of one container). Nothing is
   persisted and no URL is ever sent.

Engines without native token-level streaming generate per segment, so first-byte
time is driven by chunk size — short leading sentences play sooner.

### Device session (lugo protocol)

`WS /v1/lugo/stream`, spoken by RPi and ESP32. The device authenticates with its
pairing token; a device **must** be bound to a profile or the gateway hard-denies
the connection. Opus playback pacing is per-connection: `SessionRuntimeConfig.opus_pace`
overrides the global value when set, and `None` (what `routes/lugo.py` always
passes) inherits it — the web client sends `?opus_pace=0` to disable server-side
throttling and rely on the browser's own `AudioContext` scheduling instead of the
~300ms cushion sized for device ring buffers.

## The audio contract

Streaming STT is fixed to **PCM signed-16, mono**, at the configured stream sample
rate (admin System tab; 16 kHz default). Clients resample at the edge. TTS engines
emit different rates (VieNeu 48k, OmniVoice 24k), so re-encoding must resample —
`core/audio.py: wav_file_to_pcm16`.

`libopus` is a system library. On macOS opuslib cannot find Homebrew's copy unless
`DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib` is set (the Makefile exports it);
Opus code degrades to PCM16 when it is absent.

## Satellite services

Four services live in `servers/` beside the gateway. Their wiring status differs
and is worth knowing before you go looking for the integration:

| Service | Status |
|---|---|
| `mcp-basic-tools` | **Wired** — shipped as an MCP preset, disabled by default |
| `livehost-api` | **Wired** — registered through `/v1/plugins`; it used to live in this repo as `routes/livehost.py`, which is why comments across the codebase still reference it |
| `voiceprint-api` | **Not wired** — standalone, no gateway reference |
| `knowledge-api` | **Not wired** — standalone RAG service, gateway integration deferred |
| `router-memory-services` | **Not wired** — a standalone memory gateway that overlaps `services/memory` above. Two memory implementations currently coexist; they will diverge until one is chosen |

## Upgrade paths (not yet implemented)

- **Event bus → Redis Pub/Sub + streams** for multi-worker scale and reconnect
  replay across processes. `InMemoryEventBus` is the seam — but resolve the
  no-consumer question above first.
- **PostgreSQL** — `settings.database_url` already carries it; the sync config
  stores are the part that needs work.
- **Reliability** — queue, retries, timeouts, circuit breakers around model calls.
- **Observability** — first-chunk latency, real-time factor, error rates, and
  structured tracing keyed by `session_id`.
