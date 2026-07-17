# Local Model Service — STT/TTS as an OpenAI-compatible container

Date: 2026-07-17
Status: approved, ready for implementation plan

## Problem

Today the gateway loads STT/TTS models in-process. Every engine's weights live in the
gateway's RAM, engines can't be scaled or restarted independently, and a GPU box can't be
shared across gateway instances.

We want to run engines as **services**: a container that owns the model and speaks a
standard HTTP API, with the gateway holding nothing but a `base_url`. In the Model Registry
this is an entry with a `base_url`, not a self-hosted model.

## Scope

v1 covers **STT and TTS only**.

LLM is excluded: the gateway already reaches any OpenAI-compatible endpoint through
`OpenAICompatResponder` (`services/conversation/responder.py:152`), so a local LLM container
would be a thin proxy over Ollama/vLLM with no added value.

OmniVoice is excluded from v1: `system_config.py:49` hardcodes
`omnivoice_path = "/Users/lugon/code/OmniVoice"`, a dev-machine path, so `available()` is
always False in a container. Packaging it is separate work.

## Architecture

A new package `apps/model_service/` — a thin FastAPI app with no DB, no session auth, and no
registry. It reads env to learn which engine it is, calls the repo's existing provider, and
returns OpenAI-shaped responses.

```
SERVICE_KIND=stt              # stt | tts
SERVICE_ENGINE=whisper_local
SERVICE_API_TOKEN=<required>
ARTIFACTS_DIR=/tmp/artifacts
STT_WHISPER_LOCAL_DEFAULT_MODEL=vinai/PhoWhisper-medium
STT_WHISPER_LOCAL_DEVICE=cuda
```

The gateway then holds a registry entry: `kind=stt`, `engine=openai_stt`,
`base_url=http://stt-service:8100/v1`, `api_key=<token>`.

### Why this is cheap

The providers are already shaped for it. None of the eight target providers defines an
`__init__` — they are stateless and read config per-call through exactly four functions in
`app/services/model_registry/resolve.py`.

When no DB is present the registry store's cache stays cold, `find_sync`/`find_enabled_sync`
return `None` (`store.py:119-132`), and `resolve.py` falls back to hardcoded defaults. This
path **never raises** — it is the same code path as "no matching row". Importing
`stt.service` or `tts.service` triggers no DB access, no settings load, and no model warmup.

So the container needs no gateway DB.

### The env seam

Add an env layer to `resolve.py:41-63` with precedence **registry row > env > defaults**:

```python
{**STT_ENGINE_CONFIG_DEFAULTS[engine], **_from_env(engine), **config}
```

In the container the registry layer contributes `{}` (cold cache), so env wins automatically
— no conditional code. In the gateway a registry row exists, so registry wins and **existing
behavior is unchanged**. No provider changes.

`resolve_omnivoice_config` (`resolve.py:66-75`) gets the same treatment for free by making
`OmnivoiceConfig` a `BaseSettings` with `env_prefix="OMNIVOICE_"`.

### Removing SQLite from the hot path

Three providers read `system_config_store.get()` on every call:

- `whisper_provider.py:118` and `whisper_mlx_provider.py:58` — only for `stt_glossary_path`
- `vieneu_provider.py:71` — only for `default_tts_engine_voice`

All three values are inert at their defaults, but the first call creates a SQLite file inside
the container and raises `OperationalError` on a read-only rootfs. Move these three reads
into the `resolve.py` layer so SQLite leaves the provider hot path entirely.

`artifacts.py:81` runs `mkdir` at import time. No code change — set `ARTIFACTS_DIR`, which is
already env-backed via `settings.artifacts_dir` (`settings.py:45`).

## Components

### Container — `apps/model_service/`

`main.py` reads env, builds the app, and mounts only the router matching `SERVICE_KIND`.
A missing `SERVICE_API_TOKEN` or an unknown `SERVICE_ENGINE` **fails at startup**, not on
first request.

`auth.py` — a FastAPI dependency comparing the `Authorization: Bearer` token with
`secrets.compare_digest`.

`routes_stt.py` / `routes_tts.py` — thin adapters over
`stt_service.get_provider(SERVICE_ENGINE)` and the TTS equivalent. No logic beyond shape
translation.

API:

| Endpoint | Auth | Behavior |
|---|---|---|
| `GET /health` | no | liveness for Docker healthcheck |
| `GET /v1/models` | yes | returns the single running engine |
| `POST /v1/audio/transcriptions` | yes | multipart `file`/`model`/`language` → `{"text": ...}` |
| `POST /v1/audio/speech` | yes | JSON `{model, input, voice, response_format}` → WAV bytes |

The request's `model` field is ignored — the container is pinned by env. A `model` that
disagrees with the running engine returns 400 rather than silently doing the wrong thing.

### Gateway — two new engines

**`openai_stt`** reuses the existing `RemoteWhisperProvider`, which already POSTs to
`{base_url}/audio/transcriptions`. Today it is hardwired into two singletons with base_url
captured at construction (`stt/service.py:26-39`). The new engine resolves `base_url`/`api_key`
**per call** from the registry, mirroring `OpenRouterSttProvider._resolve_api_key`
(`openrouter_provider.py:37-41`). Per-call resolution means admin edits need no
`reinit_remote_providers()` branch.

**`openai_tts`** is new — `RemoteOpenAITTSProvider`, POSTing `{base_url}/audio/speech`, also
resolving per call.

The names describe the *protocol*, not the backend: the same entry can point at any
OpenAI-compatible server, not just this container.

### Gateway fixes required

- `routes/model_registry.py:102` whitelists `base_url` for `("llm", "stt")` only, so a TTS
  entry **silently loses its `base_url`** on create. Add `tts`. Without this the whole TTS
  path is unusable.
- `stt/service.py:121-148` hardcodes `mode: local|remote` in an if/elif chain — add
  `openai_stt` to the remote branch.
- `create_entry` (`routes/model_registry.py:69-92`) live-tests before persisting; the test
  branch must construct the provider from the payload instead of fetching the singleton, so a
  bad token or base_url surfaces when the admin clicks Add.

## Data flow

```
mic → gateway → openai_stt provider
    → POST http://stt-service:8100/v1/audio/transcriptions  (Bearer <api_key>)
    → container → whisper_local.transcribe_bytes()
    → {"text": ...} → gateway
```

## Error handling

The container maps provider failures to OpenAI-shaped errors: `EngineNotFoundError` → 400,
`ProviderError` → 502, missing/bad token → 401, unreadable audio → 400. Errors use the
OpenAI envelope `{"error": {"message", "type"}}` so any OpenAI client can read them.

The container never retries internally — retries belong to the caller, which has the timeout
budget and the request context.

On the gateway side, remote calls honor the entry's `config.timeout_seconds`, matching the
existing `whisper_service`/`eventlab` convention (`seed.py:67`, `resolve.py:94-98`).
A transport failure surfaces as `ProviderError`, which the existing STT/TTS error paths
already handle — a service being down looks like any other engine failure, not a 500.

## Testing

Container: unit tests with a fake provider injected, covering auth (missing/wrong/right
token), kind-based route mounting (a `stt` container has no `/audio/speech`), the
model-mismatch 400, and startup validation failures. No real model loads in tests.

Gateway: unit tests for `openai_stt`/`openai_tts` against a mocked HTTP layer, asserting the
Bearer header, the resolved base_url, and error translation. Plus a regression test for the
`base_url`-dropped-on-create bug — that one is TDD: failing test first.

Integration (not in CI): compose up the container with `vosk` (small, CPU-only), register it
in the gateway, and transcribe a sample WAV end-to-end.

## Docker

`infra/docker/Dockerfile.model_service`, modelled on the existing `Dockerfile.api`
(`python:3.11-slim`, `libsndfile1` + `libopus0`). One image; the engine is chosen at run
time by env, so deploying several engines means several containers from one image.

`infra/compose/docker-compose.yml` gains the service behind a compose profile, so the default
`docker compose up` for gateway development is unaffected.

## Out of scope

- LLM kind (gateway already reaches Ollama/vLLM directly)
- OmniVoice packaging (hardcoded dev path)
- Streaming STT over the service boundary (`open_stream` stays in-process for now)
- Auto-registration/discovery — entries are added by hand in the admin UI
