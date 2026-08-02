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

The image is engine-agnostic in its *code*: it contains the whole `apps/` tree
(both `api_gateway` and `model_service`), and which single engine actually runs
is picked entirely by environment variables at container start. Its *wheels* are
not: `PIP_EXTRAS` decides which engines' Python packages are installed, so an
image built for one engine generally can't serve another. It defaults to
`tts,opus` (vieneu + the Opus transport), which is what every compose file
predating this arg was built against.

| Engine | `PIP_EXTRAS` | `TORCH_INDEX_URL` |
|---|---|---|
| `vieneu` (TTS) — CPU | `tts,opus` (default) | — (ONNX Runtime, torch-free) |
| `vieneu` (TTS) — GPU | `tts,opus` | — (the CUDA image's torch is what switches it to the PyTorch path) |
| `vosk` (STT) | `opus` | — (torch-free) |
| `whisper_local` (STT) | `whisper,opus` | — (CTranslate2, torch-free) |
| `qwen3_asr_gguf` (STT) | `opus` + `--build-arg BUILD_QWEN3_ASR_GGUF=1` | — |
| `qwen3_asr` (STT) | `qwen3-asr-cuda,opus` | CPU build: `https://download.pytorch.org/whl/cpu` |
| `omnivoice` (TTS) | `omnivoice,opus` | CPU build: `https://download.pytorch.org/whl/cpu` |
| `voxcpm2` (TTS) | `voxcpm,opus` | CPU build: `https://download.pytorch.org/whl/cpu` |
| `qwen3_tts_*` (TTS) — CPU | `qwen3-tts,opus` | `https://download.pytorch.org/whl/cpu` |
| `qwen3_tts_*` (TTS) — GPU | `qwen3-tts,qwen3-tts-cuda,opus` | — (default CUDA wheel) |

`TORCH_INDEX_URL` exists because PyPI's default linux torch wheel bundles the
whole CUDA runtime (~2.5GB) even on a machine with no GPU; pointing it at
PyTorch's CPU index installs torch in its own layer first, and the later
`pip install ".[...]"` then finds the requirement already satisfied.

Build:

```bash
docker build -f infra/docker/Dockerfile.model_service -t model-service:dev .

# ... or an engine-specific image
docker build -f infra/docker/Dockerfile.model_service \
  --build-arg PIP_EXTRAS=whisper,opus -t model-service:whisper .
```

### GPU images

`infra/docker/Dockerfile.model_service.cuda` is the GPU sibling: same app, same
`SERVICE_KIND`/`SERVICE_ENGINE` contract, same port and healthcheck — only the
base image (`nvidia/cuda:*-cudnn-runtime-*`) and the wheels differ. Two things
make it a separate file rather than an arg: a Dockerfile can't swap its own
`FROM` per build, and `faster-whisper` needs cuBLAS + cuDNN 9 at model-load time
without shipping them in its wheel, so the stock slim image plus `--gpus` fails
with `Unable to load libcudnn_ops.so.9`. It also installs into a venv at
`/opt/venv` (Ubuntu 24.04 marks its system python externally managed), which
matters for `OMNIVOICE_PYTHON`. Running it needs the NVIDIA Container Toolkit on
the host (`docker run --gpus all`, or compose's
`deploy.resources.reservations.devices`).

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

### Qwen3-TTS (0.6B / 1.7B)

`SERVICE_ENGINE=qwen3_tts_0_6b` or `qwen3_tts_1_7b`
(`apps/api_gateway/app/services/tts/providers/qwen3_tts_provider.py`) has two
backends, auto-selected by hardware — same provider code, no config needed to
switch between them:

- **`qwen_tts`** (the baseline): runs on CPU, Apple Silicon MPS, or CUDA. This
  is what actually runs today on a native Mac dev setup (this project's local
  engines run natively, not in Docker, on Apple Silicon).
- **`faster_qwen3_tts`**: a CUDA-graph-capture fast path for real-time
  inference (https://github.com/andimarafioti/faster-qwen3-tts). It has **no
  CPU/MPS fallback** — it requires a real NVIDIA GPU (`torch.cuda.CUDAGraph`)
  — so the provider only selects it when `torch.cuda.is_available()` is true
  *and* the package is importable; everywhere else (including this project's
  Mac dev machines) it silently keeps using `qwen_tts`. `QWEN3_TTS_DEVICE` can
  force a device (e.g. `QWEN3_TTS_DEVICE=cpu` to opt out of both CUDA and the
  faster backend even on a GPU box).

Run natively (CPU/MPS — Mac dev, or any host without a GPU):

```bash
pip install ".[qwen3-tts,opus]"
SERVICE_KIND=tts SERVICE_ENGINE=qwen3_tts_0_6b SERVICE_API_TOKEN=dev-token \
  PYTHONPATH=apps/api_gateway:apps \
  uvicorn model_service.app.main:create_app --factory --port 8100
```

In Docker, that choice is the difference between the two compose files:
`docker-compose.tts-qwen3-tts-cpu.yml` (stock slim image + CPU torch wheels +
`QWEN3_TTS_DEVICE=cpu`) and `docker-compose.tts-qwen3-tts-gpu.yml`
(`Dockerfile.model_service.cuda` + `PIP_EXTRAS=qwen3-tts,qwen3-tts-cuda,opus` +
a GPU reservation). The stock slim image can never reach the fast path —
`torch.cuda.is_available()` is false inside it regardless of the host's GPU.

One non-obvious reason the GPU file installs `qwen3-tts-cuda` rather than
treating it as optional: when `faster_qwen3_tts` is *absent* on a CUDA device,
the provider loads the model with `attn_implementation="flash_attention_2"`,
which requires flash-attn built into the image. Selecting the faster backend
sidesteps that branch entirely.

### OmniVoice in a container

On a dev Mac the `omnivoice` provider shells out to a *separate* OmniVoice
checkout running its own venv — that's what `OMNIVOICE_PATH` (default
`/Users/lugon/code/OmniVoice`) and the derived `.venv/bin/python` mean, and it
exists so OmniVoice's dependency set never has to agree with the gateway's.

A single-engine container has no second venv to shell into, so it flips that
around: the image installs `omnivoice` from PyPI alongside the app
(`PIP_EXTRAS=omnivoice,opus`) and `OMNIVOICE_PYTHON` points at the container's
own interpreter — `/usr/local/bin/python` in the slim image, `/opt/venv/bin/python`
in the CUDA one. Everything else is unchanged: the provider still spawns
`omnivoice_sidecar.py` as a subprocess, the sidecar still loads the model once
and serves synth over `127.0.0.1:8762` inside the container, and the gateway
still only sees the OpenAI-compatible endpoint. The tradeoff is that a future
dependency conflict between `omnivoice` and the gateway's own packages surfaces
as a failed image build instead of being isolated by the second venv.

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
| `SERVICE_ENGINE` | yes | The engine name, e.g. `vosk`, `whisper_local`, `whisper_mlx`, `qwen3_asr`, `qwen3_asr_gguf` for STT; `vieneu`, `omnivoice`, `voxcpm2`, `qwen3_tts_0_6b`, `qwen3_tts_1_7b` for TTS. Must match a key the gateway's own `stt_service`/`tts_service` registers, **and** the image must have been built with that engine's `PIP_EXTRAS`. |
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

`omnivoice` has the same layer with **no prefix**, because its config field
names already carry the engine name: `resolve_omnivoice_config()` reads
`OMNIVOICE_PATH`, `OMNIVOICE_PYTHON`, `OMNIVOICE_DEVICE`, `OMNIVOICE_DTYPE`,
`OMNIVOICE_NUM_STEP`, and so on — one per field of `OmnivoiceConfig`
(`app/services/system_config.py`), same coercion and same
registry-row-beats-env precedence. The first two are what make the engine
runnable in a container at all; see the compose files.

## Wiring into the gateway's Model Registry

Once the container is running and reachable from the gateway (e.g. as the
`model-service` compose service, reachable at `http://model-service:8100`),
add a Model Registry entry in the gateway so the gateway can use it as a
remote engine. The registry engine to use depends on the container's
`SERVICE_KIND`:

- `SERVICE_KIND=stt` container → registry Engine `http_stt`
  (`apps/api_gateway/app/services/stt/providers/http_stt_provider.py`).
- `SERVICE_KIND=tts` container → registry Engine `http_tts`
  (`apps/api_gateway/app/services/tts/providers/http_tts_provider.py`).

(Both were called `openai_stt`/`openai_tts` until the rename; a startup
migration rewrites old rows, so existing deployments keep working.)

Both are the gateway's own OpenAI-compatible remote clients; they issue the
HTTP calls to the container and are otherwise symmetric — same `base_url`/
`api_key` shape, same test-before-add behavior. To add either:

1. Kind: `stt` or `tts`, matching the container's `SERVICE_KIND`.
2. Engine: `http_stt` (for `stt`) or `http_tts` (for `tts`).
3. `model_id`: whatever you want to label the model as (it's forwarded to the
   container as the `model` field but the container's engine, chosen by its
   own `SERVICE_ENGINE`, decides what actually runs — the registry `model_id`
   doesn't select the container's engine).
4. `base_url`: `http://model-service:8100/v1` (or `http://<host>:8100/v1` for
   a container reachable by hostname/IP from wherever the gateway runs).
5. `api_key`: the exact value you set as the container's `SERVICE_API_TOKEN`.

Saving the entry triggers the gateway's usual test-before-add call against the
container, so a saved entry is already proof the container answered over HTTP
with the token accepted. After that, `GET /v1/stt/engines` (for `http_stt`)
or `GET /v1/tts/engines` (for `http_tts`) on the gateway should list the
engine with `mode: remote` and `available: true`, and any request routed with
that engine reaches the container instead of loading a model in the gateway's
own process.

## Limits (v1)

This is a first version and intentionally narrow. It does not support:

- **LLM services.** There is no `SERVICE_KIND=llm`; only `stt` and `tts` exist.
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

`infra/compose/` holds one file per deployable unit. Two of them are
multi-service (local dev, and a plain-SSH VM); the rest are single-engine apps,
one per file, so each deploys as its own Coolify resource and can be
sized/restarted independently:

| File | Engine | Hardware |
|---|---|---|
| `docker-compose.yml` | api + redis + two model services | local dev |
| `docker-compose.vm.yml` | `qwen3_asr_gguf` + `vieneu` | one CPU VM, plain `docker compose` |
| `docker-compose.stt-qwen3-asr-gguf.yml` | `qwen3_asr_gguf` | CPU (quantized C++ runtime; no GPU twin by design) |
| `docker-compose.tts-vieneu.yml` + `docker-compose.tts-vieneu-gpu.yml` | `vieneu` | CPU / NVIDIA |
| `docker-compose.stt-whisper-turbo-{cpu,gpu}.yml` | `whisper_local` @ `large-v3-turbo` | CPU / NVIDIA |
| `docker-compose.stt-qwen3-asr-{cpu,gpu}.yml` | `qwen3_asr` (HF weights, non-GGUF) | CPU / NVIDIA |
| `docker-compose.tts-omnivoice-{cpu,gpu}.yml` | `omnivoice` | CPU / NVIDIA |
| `docker-compose.tts-voxcpm2-{cpu,gpu}.yml` | `voxcpm2` | CPU / NVIDIA |
| `docker-compose.tts-qwen3-tts-{cpu,gpu}.yml` | `qwen3_tts_0_6b` / `qwen3_tts_1_7b` | CPU / NVIDIA |

(The `vieneu` CPU file has no `-cpu` suffix because it is a live Coolify
resource that predates the pairs; renaming it would orphan that app.)

Every single-engine file takes `SERVICE_API_TOKEN` with no default
(`${SERVICE_API_TOKEN:?...}`), so compose refuses to start rather than quietly
booting a container that would then refuse to run. They use `build.context: "."`
(repo root) because Coolify runs `docker compose` with `--project-directory` set
to the app's base directory; building one locally needs the same base:

```bash
SERVICE_API_TOKEN=dev-token docker compose \
  -f infra/compose/docker-compose.stt-whisper-turbo-cpu.yml \
  --project-directory . up -d --build
```

The `-cpu`/`-gpu` pairs differ in three places: the Dockerfile (slim vs. `.cuda`),
the engine's device/precision env, and a `deploy.resources` block (cpu+memory
limits vs. an NVIDIA device reservation). They also mount a named `hf-cache`
volume at `/root/.cache/huggingface` — none of these engines bake weights into
the image, so the first request downloads them and the volume is what keeps a
redeploy from doing it again.

In the local-dev `docker-compose.yml`, `MODEL_SERVICE_TOKEN` plays the same role
for both `model-service-stt` (qwen3_asr_gguf) and `model-service-tts` (vieneu).

## Cloudflare Containers

`infra/cloudflare/model-service/` has a Worker + Container config
(`wrangler.jsonc` + `src/index.ts`) that deploys this same image
(`infra/docker/Dockerfile.model_service`) to Cloudflare's Containers
platform, `SERVICE_ENGINE=vieneu`. Deployed and end-to-end verified
(2026-07-19): `https://lugo-model-service-vieneu.zzitorez.workers.dev`
answers `/health` and returns real synthesized WAV audio from
`POST /v1/audio/speech`, with auth enforced (unauthenticated requests get
`401`). See that directory's own README for deploy steps, cost/instance-type
notes, and its take on `omnivoice` (which is now deployable in principle — the
env-override gap that used to block it is gone — but is a GPU-shaped,
multi-GB-image engine, so nothing about that has been tried there). Not yet
wired into this gateway's own Model Registry.
