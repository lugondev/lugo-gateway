# Profile pre-flight health check

## Problem

`http_stt`/`http_tts` route to a local, self-hostable `apps/model_service`
process over plain HTTP (`base_url` from a Model Registry row). Nothing
verifies that process is actually running before a session starts:

- Session start (`conversation.py`'s `stt_service.get_provider(...)` /
  `tts_service.get_provider(...)` gate) only checks that the engine **name**
  is a registered provider class — it never touches the network.
- `STTService.list_engines()`'s `http_stt` branch reports `available` as
  `bool(base_url)` on the enabled registry row — no network call.
- `HttpTtsProvider` never overrides `TTSProvider.available()`, so
  `TTSService.list_engines()` always reports `http_tts` as `available: true`,
  even with a dead host or no registry row at all. This is a pre-existing bug
  in the admin dashboard, not just a session-start gap.
- The first sign of trouble today is mid-conversation: the user speaks, the
  gateway calls `HttpSttProvider.transcribe_bytes`, `httpx` raises
  `ConnectError`, and the client gets `{"event": "error", "message": "STT
  failed: http_stt request failed: All connection attempts failed"}` — after
  the user already spoke.

Goal: reject a session up front, before the user says anything, when the
profile's resolved STT or TTS engine is genuinely unavailable — while not
blocking on local engines that are merely still warming up, and without
adding live network probes for engines that don't have a cheap way to
support them (paid cloud APIs).

## Status model

Every engine check resolves to one of three statuses:

- **`ok`** — safe to use now.
- **`not_ready`** — a local in-process engine that hasn't finished
  `warm()` yet (per `app.services.warmup.is_ready`). `warmup.py` marks a
  provider ready once its warm attempt *finishes*, whether or not it
  succeeded — so `not_ready` really only means "still loading", not "known
  broken". **Does not block** the session; the existing `stt_ready`/
  `tts_ready` fields in the `session_started` WS event already surface this,
  no new plumbing needed for it.
- **`unavailable`** — not configured (no registry row / no `base_url` / no
  `api_key`, matching each engine's existing `list_engines()` semantics), or
  — for `http_stt`/`http_tts` specifically — the live reachability probe
  failed. **Blocks** the session: WS is rejected before any audio is
  accepted.

Only `http_stt`/`http_tts` get a real network probe. Every other engine
(local in-process, or cloud/API-key-based like `qwencloud`/`whisper_or`/
`qwen3_asr_or`) keeps exactly its current `list_engines()` config-check
semantics — no new per-provider logic, no live API calls (avoids burning
quota/cost on every session start for paid providers that have no cheap
health endpoint).

## Components

**New:**

1. `apps/api_gateway/app/services/model_registry/health_probe.py`
   `async def probe_service_health(base_url: str, api_key: str, timeout: float = 3.0) -> tuple[bool, str | None]`.
   Does `GET {base_url with trailing "/v1" stripped}/health`. Distinguishes
   connection-level failure (`httpx.ConnectError`/`ConnectTimeout`/
   `ReadTimeout` → `(False, reason)`) from *any* HTTP response, even a 404 or
   401 (→ `(True, None)`) — the check is "is a process actually listening
   and answering", not "does this exact `/health` route exist", so it
   degrades gracefully against a non-`model_service` OpenAI-compatible host
   that doesn't implement `/health`.

2. `STTService.check_engine(engine: str, model: str | None) -> EngineHealth`
   (`apps/api_gateway/app/services/stt/service.py`) and
   `TTSService.check_engine(engine: str, model_id: str | None) -> EngineHealth`
   (`.../tts/service.py`). Internals reuse each engine's existing
   `list_engines()` per-engine branch logic (extracted into a small
   engine-keyed helper so `list_engines()` and `check_engine()` share one
   source of truth, not a duplicate) to get the base configured/available
   bool, then:
   - `http_stt`/`http_tts` and configured → resolve the entry's
     `base_url`/`api_key` and call `probe_service_health`.
   - any other engine and configured, and the provider needs warming
     (`warmup._needs_warming`) → check `is_ready(provider)`.
   - not configured → `unavailable`.

3. `apps/api_gateway/app/schemas/health.py` —
   `EngineHealth{engine, status: Literal["ok","not_ready","unavailable"], detail}`
   and `ProfileHealth{profile, stt: EngineHealth, tts: EngineHealth}`.

4. `apps/api_gateway/app/services/health.py` —
   `async def check_profile_health(profile_name: str) -> ProfileHealth`.
   Resolves engine/model exactly the way `conversation.py` already does
   (`resolve_stt(profile, ...)` for STT; `tts_profile_store` lookup via
   `profile.tts.profile_name` for TTS) so the HTTP endpoint and the WS gate
   can never disagree about what a profile resolves to. Runs the STT and TTS
   checks concurrently via `asyncio.gather` — they're fully independent
   (different registry rows, different network targets), so this halves
   worst-case latency (~3s instead of ~6s if both are remote and both hit
   the full timeout).

5. `GET /v1/profiles/{name}/health` (new route, `profiles.py`) → returns
   `ProfileHealth`. Lets the admin UI show a profile's health before a user
   even tries to connect.

**Changed:**

- `conversation.py`'s existing gate (currently just
  `stt_service.get_provider(...)` / `tts_service.get_provider(...)` inside a
  `try/except EngineNotFoundError`, ~line 346) adds
  `stt_health, tts_health = await asyncio.gather(stt_service.check_engine(stt_engine, stt_model), tts_service.check_engine(tts_engine, tts_model_id))`
  using the engine/model it has already resolved a few lines above — no
  re-resolution. If either status is `unavailable`, send
  `{"event": "error", "message": "<engine> is unavailable: <detail>"}` and
  close, same shape as today's `EngineNotFoundError` path.
- `lugo.py`'s `lugo_stream` gets the equivalent gate at the same point in its
  connection setup.
- `HttpTtsProvider` gets an `available()` override
  (`bool(base_url)` from the enabled registry row, mirroring what
  `http_stt`'s `list_engines()` branch already does) — fixes the pre-existing
  false-`available: true` bug in `GET /v1/tts/engines` as a side effect of
  building `check_engine()`'s config-check path.

## Data flow

```
WS connect (conversation.py / lugo.py)
  -> resolve profile, resolve (stt_engine, stt_model), (tts_engine, tts_model_id)   [existing]
  -> asyncio.gather(
       stt_service.check_engine(stt_engine, stt_model),
       tts_service.check_engine(tts_engine, tts_model_id),
     )
       each check_engine():
         -> configured? (reused list_engines() per-engine logic)
              no  -> EngineHealth(unavailable, "not configured")
              yes -> engine in (http_stt, http_tts)?
                       yes -> resolve registry entry's base_url/api_key
                              -> probe_service_health(base_url, api_key)
                                   fail -> EngineHealth(unavailable, "<engine> unreachable: <reason>")
                                   ok   -> EngineHealth(ok)
                       no  -> needs warming?
                                yes -> is_ready(provider)? -> ok : not_ready
                                no  -> ok
  -> any status == unavailable? -> send {"event":"error","message":...}; close WS
  -> else -> proceed to ConversationSession.start() as today
```

`GET /v1/profiles/{name}/health` runs the same `check_profile_health()` and
returns the `ProfileHealth` JSON directly — no gating, just reporting.

## Error handling

- `check_engine()` never raises for a down/misconfigured engine — failure is
  expressed as `EngineHealth(status="unavailable", detail=...)`, not an
  exception. This matches the existing pattern where `list_engines()`
  degrades per-engine rather than 500ing the whole call.
- `probe_service_health()` catches only `httpx.HTTPError` subclasses;
  anything else (programming error) still propagates, since that's a real
  bug worth surfacing loudly rather than silently reporting `unavailable`.
- If a profile references an engine name that isn't registered at all
  (shouldn't happen via normal profile editing, but possible via a stale
  reference), `check_engine()` is only ever called with an engine/model
  the caller already validated exists as a provider key (same precondition
  the current `get_provider()` gate already assumes) — no new failure mode
  introduced there.
- The WS reject path reuses the exact `{"event": "error", "message": ...}`
  shape already used for `EngineNotFoundError`, so no client-side protocol
  change is needed beyond reading the message text.

## Testing

- Unit tests for `probe_service_health()`: connection refused/timeout →
  `(False, reason)`; any HTTP response (200, 404, 401) → `(True, None)`.
  Mirrors the httpx-mocking approach already used in
  `test_http_stt_provider.py`/`test_http_tts_provider.py`.
- Unit tests for `STTService.check_engine()` / `TTSService.check_engine()`
  per branch: not-configured → `unavailable`; configured local engine not
  yet warm → `not_ready`; configured local engine warm → `ok`; `http_stt`/
  `http_tts` configured but probe fails → `unavailable`; probe succeeds →
  `ok`.
- Unit test for the `HttpTtsProvider.available()` fix (`bool(base_url)`).
- WS integration test: connecting with a profile whose STT (or TTS) engine's
  `check_engine()` returns `unavailable` gets `{"event": "error", ...}` and
  the socket closes without ever reaching `ConversationSession.start()`.
- HTTP test for `GET /v1/profiles/{name}/health` returning the expected
  `ProfileHealth` shape for a healthy and an unhealthy profile.
