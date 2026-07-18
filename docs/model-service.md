# Model service

`apps/model_service` packages a single speech engine — one STT engine or one TTS
engine, never both — as its own HTTP service, speaking an OpenAI-compatible API
and requiring a bearer token on every request. It exists so that a speech engine
can run on different hardware from the gateway: a Whisper model that wants a GPU
box, or a CPU-bound engine you'd rather isolate onto its own container so it
can't starve the gateway's event loop. The gateway then talks to it exactly like
it talks to any other remote provider — through a Model Registry entry with a
`base_url` and an `api_key` — instead of loading the model in its own process.

Concretely: today the gateway's STT and TTS engines (whisper, vosk, vieneu, and
so on) run in-process, inside the same Python process that serves the WebSocket
audio pipeline. The model service takes one of those same engines — reusing the
gateway's own provider classes, not a reimplementation — and serves it alone,
behind auth, in its own container. The gateway then reaches it over HTTP the
same way it would reach a paid third-party STT/TTS API.

This document explains how to build and run that container, how to configure
it, how to wire it into the gateway's Model Registry, and what it deliberately
does not do yet.

## Image and container

The image is built from `infra/docker/Dockerfile.model_service`. It follows the
same base and system-dependency choices as the gateway's own
`infra/docker/Dockerfile.api` (`python:3.11-slim`, `libsndfile1` for
`soundfile`/VieNeu-TTS audio I/O, `libopus0` for the Opus transport), plus one
addition specific to this image: `libatomic1`. Without it, `vosk`'s compiled
`libvosk.so` fails to `dlopen` on a slim Debian base with
`OSError: ... libatomic.so.1: cannot open shared object file` — a real failure
discovered while testing this image end-to-end with the `vosk` engine, not a
theoretical one.

The image is engine-agnostic: it contains the whole `apps/` tree (both
`api_gateway` and `model_service`), and which single engine actually runs is
picked entirely by environment variables at container start, not by anything
baked into the build. Build it once, run it many times with different env.

Build:

```bash
docker build -f infra/docker/Dockerfile.model_service -t model-service:dev .
```

Run directly (STT, vosk):

```bash
docker run --rm -p 8100:8100 \
  -e SERVICE_KIND=stt \
  -e SERVICE_ENGINE=vosk \
  -e SERVICE_API_TOKEN=dev-token \
  -e STT_VOSK_MODEL_PATH=/models/stt/vosk-model-small-en-us-0.15 \
  -v "$(pwd)/models:/models:ro" \
  model-service:dev
```

Run directly (TTS, vieneu):

```bash
docker run --rm -p 8100:8100 \
  -e SERVICE_KIND=tts \
  -e SERVICE_ENGINE=vieneu \
  -e SERVICE_API_TOKEN=dev-token \
  model-service:dev
```

The process validates `SERVICE_KIND`, `SERVICE_ENGINE`, `SERVICE_API_TOKEN`,
and `SERVICE_PORT` at startup, not on first request: an unset or misspelled
`SERVICE_KIND`/`SERVICE_ENGINE`, a missing `SERVICE_API_TOKEN`, or an
out-of-range `SERVICE_PORT` makes the container exit immediately with a
`ConfigError` rather than starting up and failing later on the first real
audio request. In particular, **`SERVICE_API_TOKEN` is mandatory** — there is
no "open" mode — so the container never accidentally serves an
unauthenticated model on the network.

That startup check does not extend to the per-engine environment layer
described below (`STT_{ENGINE}_{KEY}`, resolved via
`apps/api_gateway/app/services/model_registry/resolve.py`): those resolvers
run lazily, on the first request that needs them, not at process start. A bad
value there (e.g. `STT_WHISPER_LOCAL_VAD_FILTER=banana`) still fails loudly —
`resolve.py` raises a clear error naming the offending variable and value —
but only once something actually calls the resolver, not when the container
boots.

## Environment variables

| Variable | Required | Meaning |
|---|---|---|
| `SERVICE_KIND` | yes | `stt` or `tts`. Picks which router (transcription vs. speech) the app mounts. |
| `SERVICE_ENGINE` | yes | The engine name, e.g. `vosk`, `whisper_local`, `whisper_mlx`, `qwen3_asr` for STT; `vieneu` for TTS. Must match a key the gateway's own `stt_service`/`tts_service` registers. |
| `SERVICE_API_TOKEN` | yes | Bearer token every request must present (`Authorization: Bearer <token>`). No default — the process refuses to start without it. |
| `SERVICE_PORT` | no (default `8100`) | Port `uvicorn` binds. |
| `ARTIFACTS_DIR` | set in the image (`/tmp/artifacts`) | The gateway's artifact store creates this directory at import time even though the model service never serves artifacts; pointed at `/tmp` so nothing writes into the image's read-only working tree. You should not need to override this. |
| `DATABASE_URL` | set in the image (`sqlite+aiosqlite:////tmp/model_service.db`) | Three providers (`whisper_provider.py`, `whisper_mlx_provider.py`, `vieneu_provider.py`) still read the gateway's `system_config_store` for a few settings that are inert at their defaults in this deployment (e.g. glossary path, default voice), and that read creates a SQLite file on first use. Pointed at `/tmp` for the same reason as `ARTIFACTS_DIR`. This means those specific settings are **not configurable per-container** here — see Limits below. |

On top of those, there's a second, per-engine environment layer inherited from
the gateway's Model Registry resolver
(`apps/api_gateway/app/services/model_registry/resolve.py`). For local STT
engines, `resolve_stt_engine_config(engine)` and `resolve_stt_local_device(engine)`
read `STT_{ENGINE}_{KEY}` (env var names are always upper-cased) for every key
that engine has a default for, and that env value wins whenever there's no
Model Registry row to override it — which, inside this container, is always,
since the container has no registry database of its own. Two concrete
examples:

- `STT_WHISPER_LOCAL_DEFAULT_MODEL` — overrides `whisper_local`'s default model
  (e.g. `large-v3-turbo`).
- `STT_WHISPER_LOCAL_DEVICE` — overrides the compute device (`cpu`, `cuda`, ...)
  `whisper_local` runs on.

The same pattern applies to every key in `STT_ENGINE_CONFIG_DEFAULTS` for each
engine — for `vosk` that's `STT_VOSK_MODEL_PATH` (used in the examples above),
for `whisper_mlx` it's `STT_WHISPER_MLX_MODEL_PATH`, and so on. A typo'd env
var name is silently ignored rather than injecting an unknown key, so double
check spelling against `STT_ENGINE_CONFIG_DEFAULTS` in `resolve.py` if an
override doesn't seem to take effect.

## Wiring into the gateway's Model Registry

Once the container is running and reachable from the gateway (e.g. as the
`model-service` compose service, reachable at `http://model-service:8100`),
add a Model Registry entry in the gateway so the gateway can use it as a
remote engine. The registry engine to use depends on the container's
`SERVICE_KIND`:

- `SERVICE_KIND=stt` container → registry Engine `openai_stt`
  (`apps/api_gateway/app/services/stt/providers/openai_stt_provider.py`).
- `SERVICE_KIND=tts` container → registry Engine `openai_tts`
  (`apps/api_gateway/app/services/tts/providers/openai_tts_provider.py`).

Both are the gateway's own OpenAI-compatible remote clients; they issue the
HTTP calls to the container and are otherwise symmetric — same `base_url`/
`api_key` shape, same test-before-add behavior. To add either:

1. Kind: `stt` or `tts`, matching the container's `SERVICE_KIND`.
2. Engine: `openai_stt` (for `stt`) or `openai_tts` (for `tts`).
3. `model_id`: whatever you want to label the model as (it's forwarded to the
   container as the `model` field but the container's engine, chosen by its
   own `SERVICE_ENGINE`, decides what actually runs — the registry `model_id`
   doesn't select the container's engine).
4. `base_url`: `http://model-service:8100/v1` (or `http://<host>:8100/v1` for
   a container reachable by hostname/IP from wherever the gateway runs).
5. `api_key`: the exact value you set as the container's `SERVICE_API_TOKEN`.

Saving the entry triggers the gateway's usual test-before-add call against the
container, so a saved entry is already proof the container answered over HTTP
with the token accepted. After that, `GET /v1/stt/engines` (for `openai_stt`)
or `GET /v1/tts/engines` (for `openai_tts`) on the gateway should list the
engine with `mode: remote` and `available: true`, and any request routed with
that engine reaches the container instead of loading a model in the gateway's
own process.

## Limits (v1)

This is a first version and intentionally narrow. It does not support:

- **LLM services.** There is no `SERVICE_KIND=llm`; only `stt` and `tts` exist.
- **OmniVoice.** `OmnivoiceConfig.omnivoice_path` defaults to a hardcoded
  developer-machine path (`/Users/lugon/code/OmniVoice`,
  `apps/api_gateway/app/services/system_config.py`), which has no meaning
  inside a container, and there's no way to override it through this service's
  env layer. Don't set `SERVICE_ENGINE=omnivoice`.
- **`edge_tts`.** It isn't a `RenderingTTSProvider` (the model service's TTS
  router only serves engines that render WAV bytes synchronously) and it
  produces MP3 output rather than WAV — it can't be plugged into
  `routes_tts.py` as written. `create_app()` raises a `ConfigError` at startup
  if you try.
- **Streaming over the service boundary.** The HTTP surface is the
  OpenAI-compatible batch endpoints (`POST /v1/audio/transcriptions`,
  `POST /v1/audio/speech`) — whole file in, whole result out. Engines that
  support incremental/streaming decoding locally (e.g. `vosk`'s native partial
  results) don't expose that over this container; the gateway only gets a
  final result per call.
- **Glossary configuration in-container.** Because `DATABASE_URL` points at a
  throwaway SQLite file that starts empty every time the container starts,
  anything read from `system_config_store` — including STT glossary entries —
  is stuck at its code default inside this container. If your deployment
  needs glossary substitution, it currently has to happen on the gateway side
  after the transcription comes back, not inside the model service.

## Compose

`infra/compose/docker-compose.yml` has a `model-service` entry behind the
`models` profile, so it is *not* started by a plain `docker compose up` — that
command still only brings up `api` and `redis`, unchanged, so ordinary gateway
development doesn't suddenly try to pull model weights. Bring it up explicitly:

```bash
MODEL_SERVICE_TOKEN=dev-token docker compose -f infra/compose/docker-compose.yml --profile models up -d model-service
```

`MODEL_SERVICE_TOKEN` has no default (`${MODEL_SERVICE_TOKEN:?set MODEL_SERVICE_TOKEN}`
in the compose file) — compose refuses to start the service at all if it's
unset, rather than quietly booting the container without
`SERVICE_API_TOKEN` and having the container itself immediately refuse to run.
The default compose service mounts `../../models` (repo-root `models/`) into
the container at `/models:ro` and configures the `vosk` engine, pointed at
`STT_VOSK_MODEL_PATH=/models/stt/vosk-model-small-en-us-0.15` — that matches
the nested `models/stt/...` layout `scripts/download_vosk_model.sh` produces
by default, so running that script from the repo root is enough to make the
compose example work as written.
