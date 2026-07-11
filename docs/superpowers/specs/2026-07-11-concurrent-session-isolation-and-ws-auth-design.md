# Concurrent session isolation + WebSocket auth

## Problem

Two independent gaps prevent "many profiles/sessions streaming at once" from being safe today:

1. **STT active-model race.** `2026-07-10-stt-model-per-profile-design.md` added per-profile STT model selection but explicitly scoped out concurrency: *"the active model per engine remains a single process-global slot."* In practice this is worse than a cold-start inconvenience — it's a **live, per-turn bug**. `WhisperProvider._load_model()` (`app/services/stt/providers/whisper_provider.py:71-89`) and `Qwen3AsrProvider._mlx_session()`/`_cuda_model()` (`app/services/stt/providers/qwen3_asr_provider.py:119-141`) each call `get_active_whisper_model()` / `get_active_qwen3_asr_model()` **on every transcribe**, not just once at session start. `ConversationSession.start()` calls `apply_stt_model(cfg.stt_engine, cfg.stt_model)` (`app/services/conversation/session.py:185-191`), which mutates that same process-global. Two concurrent sessions on the same engine (e.g. both `whisper`) but different model variants will clobber each other: whichever session started (or re-warmed) most recently wins, and the other session's *next turn* silently transcribes with the wrong model — no error, no log, just a quality regression the user can't explain.

2. **No WebSocket auth.** `AuthGuardMiddleware` (`app/core/auth_guard.py`) guards HTTP paths only (`/ui`, `/static/`, `/v1/system`, `/v1/models`, `/v1/profiles`, `/v1/mcp`, `/v1/sessions`) via `request.session.get("authenticated")`. It's built on Starlette's `BaseHTTPMiddleware`, which never runs for `scope["type"] == "websocket"` — and even the guarded-prefix list doesn't include `/v1/conversation`, `/v1/stt`, or `/v1/livehost` anyway. Every WS voice endpoint is wide open regardless of `settings.admin_password`.

Both gaps block the same goal: multiple authenticated users, on different profiles, streaming independently at the same time without cross-talk.

## Scope

**In scope:**
1. Make STT model selection race-free across concurrent sessions (whisper family + qwen3_asr).
2. Add auth to the three voice WS endpoints, reusing the existing single-admin-password model.
3. A small hardening fix in `omnivoice_provider.py` (single-flight voice-ref build) — see Component 3; this is *not* the same bug class as (1), scoped down after tracing actual call sites (below).
4. Confirm (no code change) that self-hosting STT/TTS on a separate GPU box later requires zero gateway changes.

**Out of scope, with rationale:**
- Extracting STT/TTS into standalone services/containers. `RemoteWhisperProvider` (OpenAI-compatible `/audio/transcriptions`) and OmniVoice's sidecar client/server split already give this as a config change (`base_url`/`host`+`port`), not a design gap — nothing to build now.
- Parallelizing `qwen3_asr` beyond its current single dedicated MLX thread (`_INFER_EXECUTOR`, `qwen3_asr_provider.py:59-61`). MLX sessions are thread-affine (building on one thread, using from another corrupts state — see the existing comment at `qwen3_asr_provider.py:51-58`). Real concurrent throughput would require a separate OS process per concurrent stream (own memory, own model copy) — a real RAM cost with no way around it, not something to default on. `whisper`/`whisper_local` already parallelize correctly today (`asyncio.to_thread` pool + a model cache keyed per model+device+compute_type, `whisper_provider.py:12-17,66-89`), so genuine concurrent STT needs stay on that engine family.
- Per-profile OmniVoice model selection. Doesn't exist yet (`SttConfig.model` has a profile-level field; OmniVoice's TTS side has no equivalent), so there's no live session-vs-session race to fix — see Component 3.
- Deploying an actual self-hosted GPU inference server. That's an infra/ops task for whenever real hardware exists, not a code change.

## Component 1 — STT model correctness

Both providers already cache **multiple loaded models simultaneously**, keyed by model id (`whisper_provider.py:12` `_MODEL_CACHE`, keyed by `model:device:compute_type`; `qwen3_asr_provider.py:24` `_MODEL_CACHE`, keyed by `f"{backend}:{model}"`). The bug is purely that the *lookup key* comes from a mutable global re-read per call instead of being passed explicitly. This means the fix is a parameter-threading change, not a new caching/locking layer, and costs no extra memory beyond what's already cached.

Changes:

- `WhisperProvider.transcribe_bytes(self, audio_bytes, language=None, model=None)` and `_do_transcribe`/`_load_model` accept `model: str | None`; `_load_model` uses `model or get_active_whisper_model()` instead of always reading the global.
- `Qwen3AsrProvider.transcribe_bytes(self, audio_bytes, language=None, model=None)`; `_transcribe`/`_mlx_session`/`_cuda_model` take `model` explicitly, same fallback pattern.
- `STTProvider.transcribe_bytes` base signature (`app/services/stt/base.py`) gains the optional `model` param so all providers share one call shape (no-op for engines without variants, e.g. `vosk`, `whisper_mlx`).
- `ConversationSession.start()` (`session.py:185-192`) stops calling `apply_stt_model()` for session routing. It resolves the effective model id once (`cfg.stt_model or get_active_whisper_model()/get_active_qwen3_asr_model()` depending on engine, matching today's fallback semantics) and stores it as `self.stt_model_id`.
- Every call site that transcribes — the main turn path and the fast-STT-engine path (`session.py:480-497`) — passes `model=self.stt_model_id` explicitly.
- `apply_stt_model()`, `whisper_manager.select()`, `set_active_qwen3_asr_model()` are **kept**, unchanged, for the admin "System" page's model download/select flow — that's a legitimate server-wide default, and sessions that don't set `cfg.stt_model` still fall back to it via the existing `get_active_*` functions. Only the *live-session* mutation-then-reread pattern is removed.

Net effect: N concurrent sessions on the same engine but different model ids each transcribe against their own cached instance, no cross-session interference, first-load cost only (already true today for the *first* session to use a given variant).

## Component 2 — WebSocket auth

Since `AuthGuardMiddleware` cannot see WebSocket scope, the check moves into each route handler directly, via one shared helper:

```python
# app/core/auth_guard.py (new function)
def ws_authenticated(websocket: WebSocket) -> bool:
    if not settings.admin_password:
        return True  # matches today's HTTP behavior: auth off when unconfigured
    if websocket.session.get("authenticated"):
        return True  # browser: same cookie session as the HTTP UI
    token = websocket.query_params.get("device_token")
    return bool(settings.device_auth_token) and token == settings.device_auth_token
```

- New setting: `device_auth_token: str = ""` (`app/core/settings.py`) — a single shared secret for ESP32/RPi device clients, which can't do a browser cookie login. Passed as `?device_token=...` at WS connect time (query param, since embedded WS clients handle those more easily than custom headers).
- Applied at the top of all three handlers, before `await websocket.accept()`:
  - `conversation.py:conversation_stream` (`/v1/conversation/stream`)
  - `stt.py` websocket route (`/v1/stt` streaming)
  - `livehost.py` websocket route
- On failure: `await websocket.close(code=4401, reason="unauthorized")` and return — never accept.
- When `settings.admin_password` is unset (today's local/dev "auth disabled" escape hatch), WS auth is skipped too, so local development is unaffected.

## Component 3 — OmniVoice (scoped down after tracing call sites)

Initially assumed this mirrored the STT bug; tracing `set_active_omnivoice_model` usage shows it's only called from the admin route (`app/services/tts_models.py:107`), never from session/profile resolution — there is no per-profile OmniVoice model field today, so no session-vs-session clobbering is possible for `_active_model`.

`_voice_ref` (`omnivoice_provider.py:22,65-75`) is a shared cache of one synthesized reference voice, built from **global settings** (`settings.omnivoice_ref_text`, `settings.omnivoice_default_instruct`) — every session that doesn't set its own `ref_audio`/`instruct` is *supposed* to share the same pinned voice, so the shared cache is correct behavior, not a bug. The only real risk is a benign race: two sessions hitting `_ensure_voice_ref()` on a cold cache at the same moment both synthesize it (wasted work, not wrong output — last write wins, both writes are equivalent).

Fix: single-flight the build, mirroring the existing `_MODEL_LOCK` pattern in `whisper_provider.py:17,81-89`:

```python
_voice_ref_lock = asyncio.Lock()

async def _ensure_voice_ref(self) -> dict[str, str]:
    if _voice_ref.get("path") and os.path.isfile(_voice_ref["path"]):
        return _voice_ref
    async with _voice_ref_lock:
        if _voice_ref.get("path") and os.path.isfile(_voice_ref["path"]):
            return _voice_ref
        ...  # existing build logic
```

No other change needed on the TTS side.

## Self-host readiness (validated, no action needed)

Confirmed both engines already have a clean extension point for a future GPU-hosted deployment:

- **STT**: `RemoteWhisperProvider` (`remote_whisper_provider.py`) calls the standard OpenAI-compatible `POST {base_url}/audio/transcriptions`. Any self-hosted server implementing that contract (e.g. faster-whisper-server, speaches, or a small custom wrapper) becomes a drop-in engine by setting `whisper_service_base_url`/`eventlab_base_url` — zero gateway changes.
- **TTS**: OmniVoice already runs as a sidecar server (`_spawn_sidecar`, `_server_synth`, `omnivoice_server_host`/`omnivoice_server_port`, `omnivoice_provider.py:77-116`) — pointing those settings at a remote GPU box already works today.
- Component 1's fix (explicit `model` parameter instead of a global) makes the in-process providers match the calling convention the remote providers already use (stateless, parameters per call) — consistent behavior whichever provider a profile resolves to, now or after adding a GPU-hosted engine.

No architecture change is needed to prepare for this; it's already supported.

## Testing

- `WhisperProvider`/`Qwen3AsrProvider`: two concurrent `transcribe_bytes()` calls with different `model=` values assert each returns from/uses its own cached model instance (spy on `_load_model`/`_mlx_session`/`_cuda_model` args), and that the process-global `get_active_*` value is untouched by passing `model` explicitly.
- `ConversationSession`: two sessions on the same engine, different `cfg.stt_model`, run turns interleaved (`asyncio.gather`) — assert each turn's transcribe call received its own session's model id, not the other's.
- `ws_authenticated()`: unit tests for all four branches (auth disabled, valid cookie, valid device token, neither).
- WS route integration test: connect without cookie/token → closed with 4401; with valid cookie → accepted; with valid `device_token` → accepted.
- `omnivoice_provider._ensure_voice_ref`: concurrent calls on a cold cache result in exactly one synthesis (spy/count calls to the underlying `_synth`).
- Full existing suite still green (no behavior change for single-session or already-passing profile/engine combinations).
