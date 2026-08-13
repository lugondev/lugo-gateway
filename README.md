# LUGO

**LUGO** is a self-hosted **AI Companion platform** — it turns AI models into a
companion that remembers, talks, acts, and grows with you across every device, from
the browser to ESP32 and Raspberry Pi ([lugo.vn](https://lugo.vn/)).

This repository (`speech-text-transformer`) is LUGO's **gateway**: a local service
unifying Speech-to-Text, Text-to-Speech, and a voice Conversation loop
over REST and WebSocket, with a browser playground. STT: 11 engines — Vosk,
faster-whisper, Qwen3-ASR (Vietnamese), Apple-GPU MLX (`whisper_mlx`), remote
OpenAI-compatible, OpenRouter, DashScope, and the containerized model service.
TTS: 8 engines — OmniVoice, VieNeu, VoxCPM2, Kokoro-Vietnamese, Qwen3-TTS, edge-tts,
and the model service. Conversation: VAD turn-taking + barge-in, local/online LLM,
PCM or Opus transport.

Beyond the playground it runs a small multi-device voice platform: **bearer/session
auth** with users and roles, **device pairing** for hardware clients, a unified
**Model Registry** for STT/TTS/LLM engines, per-profile config with **MCP** tools and
per-user **chat memory**, and the **lugo** binary WebSocket protocol spoken by the
Raspberry Pi and ESP32 clients.

## Repository & submodules

This repo uses **nine git submodules**, so clone recursively (or init after cloning):

```bash
git clone --recurse-submodules https://github.com/lugondev/speech-text-transformer.git
# or, in an existing clone:
git submodule update --init --recursive
```

Clients — they talk to this gateway:

| Path | Repo | What |
|---|---|---|
| `rpi-assistant` | lugondev/rpi-assistant | Raspberry Pi voice client (lugo protocol) |
| `esp32-assistant` | lugondev/esp32-assistant | ESP-IDF firmware thin client |
| `lugo-web-client` | lugondev/lugo-web-client | React SPA web client (bearer auth) |
| `lugo-landing` | lugondev/lugo-landing | Marketing site (Vite + React) |

Services — they run beside the gateway:

| Path | Repo | What | Wired in? |
|---|---|---|---|
| `servers/mcp-basic-tools` | lugondev/mcp-basic-tools | Built-in MCP tools server (web_search, fetch, …) | yes, via MCP presets |
| `servers/livehost-api` | lugondev/livehost-api | TikTok Live co-host, an out-of-process **plugin** | yes, via `/v1/plugins` |
| `servers/voiceprint-api` | lugondev/voiceprint-api | 3D-Speaker voiceprint service | not yet |
| `servers/knowledge-api` | lugondev/knowledge-api | Knowledge-base RAG service | not yet |
| `servers/router-memory-services` | lugondev/router-memory-services | Standalone memory gateway | not yet |

"Not yet" means the service is standalone and complete but the gateway holds no
reference to it — see [docs/architecture.md](docs/architecture.md#satellite-services).

## Quick start

### With the Makefile (recommended)

```bash
make install     # venv + deps
make dev         # run in foreground with --reload
# or run/manage as a background service:
make start       # start (logs -> .run/gateway.log)
make status      # is it running?
make stop        # stop
make restart     # restart
make test        # pytest
make help        # all targets
```

### Local Python

```bash
cd /Users/lugon/code/speech-text-transformer
python -m venv .venv
source .venv/bin/activate
pip install -e .
PYTHONPATH=apps/api_gateway uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Optional extras: `pip install -e ".[mlx]"` (Apple-Silicon GPU STT: `whisper_mlx`),
`pip install -e ".[opus]"` (Opus audio transport — also needs system
libopus: `brew install opus` / `apt install libopus0`).

### What can this machine run?

```bash
make check-system            # or: python scripts/check_system.py [--json]
```

Scans the hardware (CPU/RAM/disk, Apple Silicon vs NVIDIA, GPU + VRAM, Docker +
NVIDIA Container Toolkit, audio libs), validates the setup, and recommends the
engine stack — plus the exact `infra/compose/*.yml` file for this host, CPU or
GPU variant. Stdlib-only, so it also runs on a bare box before anything is
installed.

### Setup local Vosk and Whisper

```bash
cd /Users/lugon/code/speech-text-transformer
./scripts/setup_local_stt.sh
```

This script installs dependencies, downloads a Vosk model, and warms up the local faster-whisper model.

### Docker Compose

```bash
cd /Users/lugon/code/speech-text-transformer/infra/compose
docker compose up --build
```

## Documentation

- [docs/api.md](docs/api.md) — full REST / WebSocket reference and schemas.
- [docs/device-integration.md](docs/device-integration.md) — **Raspberry Pi / ESP32
  voice device guide**: protocol, audio formats, and a runnable reference client.
- [docs/architecture.md](docs/architecture.md) — components, data flows, upgrade paths.
- [apps/model_service/README.md](apps/model_service/README.md) — the one-engine-per-container
  model service, its per-engine CPU/GPU compose files, and the `http_stt` /
  `http_tts` remote providers.
- [docs/runbook.md](docs/runbook.md) — run, configure, troubleshoot.
- [docs/decisions.md](docs/decisions.md) — choices made deliberately, what they
  were made against, and what would change them. Worth reading before treating
  something absent as an oversight.

## Endpoints

- GET /health
- GET /ui
- GET /agents-docs (AGENTS.md + all docs bundled as markdown, for coding agents)
- GET /v1/stt/engines
- POST /v1/stt/transcribe
- POST /v1/stt/warm (preload a heavy STT model, e.g. whisper large)
- WS /v1/stt/stream
- POST /v1/tts/synthesize
- WS /v1/conversation/stream (voice turn-taking; `?audio_codec=pcm16|opus`)
- GET/POST /v1/conversation/llm + POST /v1/conversation/llm/reset (online LLM config)
- POST /v1/conversation/chat (text chat with the conversation responder)
- POST /v1/models/{whisper,vieneu,omnivoice,llm,...}/download|select|delete
- GET /v1/system/status
- GET /v1/models
- POST /v1/models/vosk/download
- DELETE /v1/models/vosk/{name}

Platform (auth, devices, profiles, registry, memory):

- POST /api/auth/{signup,login,logout,token,refresh,introspect} — session cookie
  or bearer token; `introspect` is how a plugin resolves a ticket it was handed
- GET/POST/PATCH /v1/users (admin) — user + role management, incl. disable
- WS /v1/lugo/stream — the **lugo** binary protocol for RPi / ESP32 clients
- POST /v1/devices/pair/{init,status,claim} — 8-digit device pairing
- GET /v1/devices, /v1/devices/mine — paired-device management + rename + revoke
- GET/POST/PUT/DELETE /v1/profiles + /v1/profiles/{name}/memories — per-profile
  config (LLM, STT/TTS, MCP servers, memory) and per-user chat memory
- GET /v1/profiles/{name}/health — pre-flight STT/TTS readiness for one profile
- GET/POST /v1/model_registry — unified STT/TTS/LLM engine registry (the active
  conversation LLM is the enabled `kind="llm"` entry)
- GET/POST /v1/providers — upstream provider accounts + their model lists
- GET/POST /v1/quotas, GET /v1/usage/{me,summary} — spend limits + usage rollups
- GET/POST /v1/mcp — global + per-profile MCP servers
- GET /v1/sessions — chat history/session store
- GET /v1/stats/home — role-scoped counts for the admin Home tab
- GET/POST/PUT/DELETE /v1/plugins + POST /v1/plugins/ticket — out-of-process
  feature plugins (e.g. `servers/livehost-api`) and the ticket a browser trades
  for a direct connection to one
- POST /v1/models/install — pip-allowlist install for optional engines

Admin console at `/ui` (same API host). The left nav has 16 sections; the ones
marked *(admin)* are hidden from a `role="user"` session:

| Section | What |
|---|---|
| **Home** | landing tab — profile/device/session counts, usage + quota, and *(admin)* system health, active models, registry summary |
| **Conversation** | live voice loop + text chat; shows the active STT/LLM/TTS. Profile editor and session history open as side panels here |
| **Devices** | own paired devices, pairing claim, profile binding, revoke; *(admin)* the all-devices table with search/filter |
| **STT** | mic record, batch transcribe, and the streaming socket |
| **TTS** | synthesize, voice picker, reference-audio upload for cloning, TTS profiles |
| **My Usage** | the caller's own spend against their quota |
| **Models** *(admin)* | download/select/delete for Vosk, Whisper, VieNeu, OmniVoice, LLM, plus the hardware recommender |
| **MCP** | global + per-profile MCP servers, tool listing, enable/clone |
| **Plugins** *(admin)* | the out-of-process plugin registry — url, shared secret, mounts, enable/disable |
| **Users** *(admin)* | accounts, roles, disable, password reset |
| **Model Registry** *(admin)* | the unified STT/TTS/LLM entry list and its defaults |
| **Providers** *(admin)* | upstream provider accounts and their model lists |
| **Usage** *(admin)* | spend rollups across all users |
| **Pricing** *(admin)* | per-model price table driving cost attribution |
| **Quotas** *(admin)* | global and per-user spend limits |
| **System** *(admin)* | status, system settings (79 config fields), VAD + denoise, remote-engine endpoints |

Feature plugins registered in `/v1/plugins` inject their own nav item at load
time — nothing about them is hardcoded in the page.

## Streaming protocols

### STT WebSocket (`/v1/stt/stream`)

Audio contract: raw PCM signed-16, mono, at the configured stream sample rate
(admin System tab; default 16 kHz).
Query params: `?engine=vosk&language=en&sample_rate=16000`.

- Client -> server: binary PCM frames, or text control `{"type":"flush"}` / `{"type":"end"}`.
- Server -> client events: `session_started`, `partial`, `final`, `error`, `done`.
- Vosk decodes incrementally (real partials + finals). Other engines buffer audio and
  return a single `final` on flush/end.

### TTS (batch)

POST `/v1/tts/synthesize` with `{text, engine}` returns the raw synthesized audio
bytes directly (`audio/wav`, or `audio/mpeg` for `edge_tts`) plus `X-TTS-*` headers
— no job/SSE indirection and no URL pointing at a file. For sentence-by-sentence
streaming playback, use the conversation socket (below); it pushes each sentence's
audio as binary WebSocket frames instead of a URL.

Every TTS request runs real synthesis; a failing engine returns a JSON error
(HTTP 502) instead of a silent placeholder.

## STT engine options

`GET /v1/stt/engines` returns the live list with an `available` flag per engine —
a heavy or optional engine reports `available: false` (with an install hint)
rather than failing at call time. 12 names, 11 distinct engines: `whisper_local`
is an alias of `whisper` and shares its provider instance.

Local:

- **vosk** — local Vosk model. Needs WAV PCM16 mono for the batch endpoint, and is
  the only engine that decodes incrementally (real partials on `WS /v1/stt/stream`).
- **whisper** / **whisper_local** — local faster-whisper, defaults to
  **large-v3-turbo**. CPU on macOS (~3.7s/utterance); CTranslate2 has no GPU there.
- **whisper_mlx** — Whisper on the Apple-Silicon **GPU** via MLX: ~0.5s/utterance
  (~7× faster than CPU), same accuracy. Mac only; auto-falls back to `whisper`
  elsewhere. Build the model with `scripts/convert_phowhisper_mlx.sh`.
- **qwen3_asr** — the **default**. Beat faster-whisper on Vietnamese (FLEURS
  benchmark). MLX on Apple Silicon, `qwen-asr` on an NVIDIA GPU.
- **qwen3_asr_gguf** — the same model on CPU, as a GGUF subprocess to
  `qwen3-asr.cpp`. For boxes with no GPU and no Apple Silicon.

Remote:

- **whisper_service** — remote OpenAI-compatible transcription endpoint.
- **eventlab** — a second remote endpoint on the same OpenAI-compatible API.
- **qwen3_asr_or** / **whisper_or** — OpenRouter-hosted `qwen3-asr-flash` and
  `whisper-large-v3-turbo`.
- **qwencloud** — Alibaba DashScope (`qwen3-asr` and `fun-asr`), both the batch
  API and the realtime WebSocket.
- **http_stt** — the one-engine-per-container model service in
  [apps/model_service](apps/model_service/README.md). Point it at any host running
  that image.

Endpoints for the remote engines (base url / API key / model) are configured in
the admin **System** tab (System settings → `remote_stt` group), not `.env`.

## TTS engine options

`GET /v1/tts/engines` returns the live list; `GET /v1/tts/voices?engine=…` returns
the voices an engine offers and whether it supports reference-audio cloning.

- **omnivoice** — 24kHz, voice cloning via reference audio. Source path set in the
  admin System tab (System settings → `omnivoice` group).
- **vieneu** — 48kHz Vietnamese, ~0.4s to first audio.
- **voxcpm2** — 48kHz, voice cloning and voice-description prompting.
- **kokoro_vi** — Kokoro-82M fine-tuned for Vietnamese, 24kHz, fixed voicepacks
  (no cloning).
- **qwen3_tts_0_6b** / **qwen3_tts_1_7b** — Qwen3-TTS, two sizes.
- **edge_tts** — Microsoft Edge voices; returns MP3 (`audio/mpeg`), not WAV.
- **http_tts** — the containerized model service, same as `http_stt` above.

Engines emit different sample rates, so anything re-encoding their output must
resample (`core/audio.py: wav_file_to_pcm16`).

## Conversation (voice)

`WS /v1/conversation/stream` runs a full voice loop: VAD endpointing → STT → reply →
streamed TTS, with barge-in. The reply comes from a text LLM — local **Ollama** or any
OpenAI-compatible **online** provider (OpenAI/Groq/Together). The active LLM is the
enabled `kind="llm"` entry in the **Model Registry** (`/v1/model_registry`, managed in
the admin UI); a profile can override it per-conversation.

It's a unified **text/audio → text/audio** gateway: input is audio frames or a
`{"type":"text"}` message; `?output=audio,text` picks what comes back — covering
audio→audio, text→audio, audio→text, text→text. Input audio is PCM16 or Opus
(`?audio_codec=opus`); reply audio is pushed as binary WebSocket frames, one
complete WAV or MP3 container per sentence (`?audio_out=wav`, the default), or as
Opus binary frames (`?audio_out=opus`, for ESP32 / Raspberry Pi) — never a URL, and
nothing synthesized is persisted to disk. See [docs/api.md](docs/api.md).

The API a `whisper_service` / `eventlab` endpoint must expose:

- POST {base_url}/audio/transcriptions
- multipart file field: file
- form fields: model, language(optional), response_format=json
- response json includes text

## Plugins

A **plugin** is a separate service that adds a feature to the console without
living in this repo — `servers/livehost-api` is the first one. The gateway keeps a
registry (`/v1/plugins`: name, url, secret, mounts) and the admin console injects a
nav item per enabled plugin at load time, so adding one needs no change to
`index.html`.

A browser reaching a plugin does not send its session cookie. It calls
`POST /v1/plugins/ticket` for a short-lived ticket, hands that to the plugin, and
the plugin resolves it against `POST /api/auth/introspect` using its registered
secret. See `docs/superpowers/specs/2026-08-05-gateway-plugin-contract-design.md`.

## Auth & multi-user

Auth turns on when an admin password is set (`ADMIN_PASSWORD` or
`ADMIN_BOOTSTRAP_PASSWORD`); otherwise it no-ops for local dev. Two identity schemes,
no fallback between them:

- **Session cookie** — the admin web UI (`/ui`), full role (admin/user).
- **Bearer token** — the React web client (`lugo-web-client`) and API clients; always
  resolves to `role="user"`, so a token can't escalate to admin.

Hardware clients authenticate the `WS /v1/lugo/stream` connection with a per-device
token from the pairing flow (`/v1/devices/pair/*`), or a shared `DEVICE_AUTH_TOKEN`
stopgap. Chat memory and profiles are scoped per user, so devices/users on a shared
template profile don't see each other's memories.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

## Notes

- OmniVoice source path is configured in the admin System tab (System settings > omnivoice group).
- Vosk requires WAV PCM16 mono input for local transcribe endpoint.
