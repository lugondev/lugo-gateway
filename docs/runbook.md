# Runbook

## Run locally

```bash
cd /Users/lugon/code/lugo-gateway
python3.12 -m venv .venv && source .venv/bin/activate   # 3.12, see the note below
pip install -e ".[dev]"
cp .env.example .env   # then edit as needed
PYTHONPATH=apps/api_gateway uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open the playground at `http://localhost:8000/ui`. Interactive API docs are at `/docs`.

> Run uvicorn from the repo root: the static mount and `ARTIFACTS_DIR` resolve relative
> to the working directory.

> **Python 3.12**, not 3.13/3.14. `pyproject.toml` only declares `>=3.10`, so nothing
> stops you creating a newer venv — it just fails later, when the spacy/ML wheels the
> STT extras need turn out not to exist for that runtime.

## Local model setup

```bash
./scripts/setup_local_stt.sh   # installs deps, downloads a Vosk model, warms faster-whisper
# or just the Vosk model:
./scripts/download_vosk_model.sh
```

Without a Vosk model, `vosk` requests return a clear error (the gateway does not crash).
Set the Vosk model path in the admin **System** tab (System settings) to use a different model.

### Apple-GPU STT (whisper_mlx, ~7x faster)

```bash
.venv/bin/pip install -e ".[mlx]"     # Apple Silicon only
./scripts/convert_phowhisper_mlx.sh   # builds models/stt/whisper-large-v3-turbo-mlx
```
Then set the conversation STT engine to `whisper_mlx` in the admin System tab (or
`conversation.conversation_stt_engine` via `PUT /v1/system/config`). The engine
auto-hides on non-Mac hosts, so callers fall back to the CPU `whisper` engine.

### Conversation LLM (local Ollama or online)

Local: run Ollama, set the conversation LLM base url/model in the System tab
(`http://localhost:11434/v1` + `gemma2:9b`, for example); manage/activate models there too.
Online: pick a provider (OpenAI/Groq/Together) in the System tab "Online" card (or
`POST /v1/conversation/llm`) — the API key is kept in memory only, never persisted.

### Opus audio transport (ESP32 / Raspberry Pi / browser)

`?audio_codec=opus` on the conversation WS streams Opus instead of PCM16 (~10x less
bandwidth). Requires the system Opus library + binding:

```bash
brew install opus        # macOS   (then run via `make` so DYLD_FALLBACK_LIBRARY_PATH is set)
sudo apt install libopus0  # Debian/Ubuntu
.venv/bin/pip install -e ".[opus]"
```
If libopus is missing the server logs a warning and falls back to PCM16.

## Docker Compose

```bash
cd infra/compose
docker compose up --build
```

Brings up the API on port 8000. (A Redis container used to ride along, reserved
for an event-bus upgrade; the bus was removed for want of a consumer, so Redis is
not part of the running system.)

## Tests

```bash
pip install -e ".[dev]"
pytest                  # ~2000 tests, ~3.5 min
ruff check apps tests   # `make lint` runs exactly this scope
```

Tests are hermetic (`tests/conftest.py` forces mock engines — no Ollama, no model
downloads). Apple-only and opus-dependent tests skip when unavailable.

> **Don't run two suites at once.** `tests/concurrency_guard.py` refuses to start
> while another pytest is alive on the machine, because two concurrent runs of this
> repo deadlock each other on the shared model/HF caches. It matches on the process
> command line, so a shell loop that merely *mentions* `python -m pytest` (a wait-for-it
> script, for instance) will also trip it — kill the stray, or set
> `PYTEST_ALLOW_CONCURRENT=1` if you are sure.

Submodules carry their own suites and their own virtualenvs; the root `pytest` does
not reach them. Run each from its own directory (`servers/knowledge-api`,
`servers/router-memory-services`, …).

## Configuration

Bootstrap settings (process identity, networking, auth, storage paths) are environment
variables (or `.env`). See `.env.example` for the full list.

| Variable | Default | Purpose |
|----------|---------|---------|
| `APP_HOST` / `APP_PORT` | `0.0.0.0` / `8000` | bind address |
| `LOG_LEVEL` | `INFO` | logging level |
| `CORS_ALLOW_ORIGINS` | `*` | comma-separated origins, or `*` |
| `TRUSTED_PROXY_HOPS` | `0` | reverse proxies you own, in front of this app — see below |
| `ADMIN_PASSWORD` | — | browser control-panel login |
| `SESSION_SECRET` | — (random per process) | signs cookie sessions **and** bearer tokens — see below |
| `ARTIFACTS_DIR` | `artifacts` | voice-clone reference audio only (never served over HTTP) — synthesized reply audio is never written to disk |

### `TRUSTED_PROXY_HOPS` — set it to 1 in production

`/api/auth/login`, `/api/auth/token`, `/api/auth/signup` and `/v1/devices/pair/*` are
rate limited, and every one of those limiters keys on the client's address.

Behind a reverse proxy the socket peer is the *proxy*, identical for every real client,
so at the default of `0` those per-client limits collapse into one shared bucket for the
whole deployment. Nothing breaks silently — logins still work — but a single noisy client
can spend everyone's budget, and device pairing (`init_rate_limiter`, 30 req/30s) is the
easiest one to exhaust.

Set it to the number of proxies **you** control (`1` for a single nginx / Traefik /
Coolify / Cloudflare in front). Only that many rightmost `X-Forwarded-For` entries are
read, because those are the ones your own proxies appended; anything to their left came
from the caller and is forgeable. Leave it at `0` when the app is exposed directly —
reading the header with no proxy in front lets any caller mint a fresh limiter key per
request and skip the limit entirely.

### `SESSION_SECRET` — set it in production before the web client ships

`SESSION_SECRET` is the HMAC key used to sign two things: the admin webui's cookie
session, and the Lugo web client's bearer access + refresh tokens (both are signed with
the same key — `settings.effective_session_secret`).

**When it is unset, the key is randomly generated at process start** (`secrets.token_hex(32)`
at import) and therefore **changes on every restart or redeploy.** Consequence: every
restart invalidates all existing sessions and tokens — admin users get logged out, and
every bearer token dies, including the 30-day refresh tokens. So `REFRESH_TTL = 30 days`
is only honest when `SESSION_SECRET` is actually set.

This is harmless in local dev (you just log in again) and it is unchanged from the old
cookie-only behavior. But `main` auto-deploys to prod, so on a server that redeploys
often it looks like the app is **randomly logging users out**. Set it once to a fixed,
secret value:

```bash
# generate a value
python -c "import secrets; print(secrets.token_hex(32))"   # or: openssl rand -hex 32
```

Set `SESSION_SECRET=<that value>` in the prod environment (Coolify > app > Environment
Variables). Keep it stable across deploys; rotating it logs everyone out on purpose.

### Prod checklist for the Lugo web client (cross-origin, bearer)

The web client is a separate app on its own domain, talking to this gateway with bearer
tokens (no cookies). Before it ships, three env-level things must be true — none are code
changes:

1. **`SESSION_SECRET`** set to a fixed value (above), or tokens die on each redeploy.
2. **HTTPS** on the web client's origin. WebCodecs and the microphone require a *secure
   context*; without HTTPS the Talk screen does not degrade — it does not work at all.
3. **`CORS_ALLOW_ORIGINS`** narrowed from `*` to the web client's real origin
   (e.g. `https://app.example.com`). `*` is acceptable only while `allow_credentials` is
   off (it is), and is fine for dev; production should name the origin.

STT/TTS engine choice, Whisper/Vosk/OmniVoice/Qwen3 model settings, remote STT provider
endpoints/keys, conversation LLM endpoint, conversation tuning, and VAD/noise
preprocessing all live in the admin UI now (**System** tab > System settings, backed by
`GET`/`PUT /v1/system/config`) — they are no longer environment variables. See
`docs/superpowers/specs/2026-07-13-env-to-admin-system-settings-design.md` for the full
field-by-field mapping from the old env vars to their new config-store location.

## Enabling real TTS

1. Ensure the OmniVoice checkout at the configured OmniVoice path (System tab) is
   importable and its dependencies (torch, etc.) are installed in the active environment.
2. Pick a device in the System tab (Apple Silicon: `mps`; NVIDIA: `cuda:0`).

Every request runs real synthesis. If the model fails to load, the provider returns
a `ProviderError` (502) instead of placeholder audio.

## VAD backends (STT preprocessing)

The VAD backend (admin System tab > preprocessing) selects how voice activity is
detected for batch transcription (`/v1/stt/transcribe`, also overridable per request
via the `vad_backend` form field). All backends run **locally** — none call an
external service at inference time.

| Backend | Install | Token | Notes |
|---------|---------|:-----:|-------|
| `energy` | built-in (numpy) | — | Always available; simple RMS gate. |
| `silero` | `pip install silero-vad` (pulls `torch`) | no | Neural VAD; weights auto-download (ungated). |
| `pyannote` | `pip install pyannote.audio` | **yes** | Neural VAD; weights are **gated** on Hugging Face. |

### Why pyannote needs a token (it is not a remote service)

pyannote.audio runs entirely on your machine. The token is **only used once to download
the model weights** from the Hugging Face Hub, because `pyannote/segmentation-3.0`
(the default pyannote VAD model, set via `PYANNOTE_VAD_MODEL`) is a **gated
repository** (HF requires you to accept the model license before downloading). After
the weights are cached locally, inference is fully offline — no per-request API call.
The VAD pipeline is built from this segmentation model (`VoiceActivityDetection`), not
the legacy `pyannote/voice-activity-detection` pipeline (incompatible with
pyannote.audio 4.x).

To enable pyannote:
1. `pip install pyannote.audio` (pulls `torch`).
2. Create a token at <https://huggingface.co/settings/tokens>.
3. Click **Agree** on `pyannote/segmentation-3.0`.
4. Set `PYANNOTE_AUTH_TOKEN` (and `PYANNOTE_VAD_MODEL`, if overriding the default) in
   the deployment's env and restart the process — these are deployment-time settings,
   not admin-editable (unlike the VAD backend choice itself).

`silero` needs no token and is the recommended neural VAD when you don't want to deal with
HF gating. If a selected backend is unavailable it falls back to `energy`.

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| WS `error`: "Vosk model not found" | run `scripts/download_vosk_model.sh` |
| `/transcribe` 400 "requires WAV PCM16 mono" | convert input, e.g. `ffmpeg -i in.mp3 -ar 16000 -ac 1 -c:a pcm_s16le out.wav` |
| TTS request fails with 502 | engine failed to load/run — check logs for the underlying error |
| Browser autoplay blocked | click a control once; chunk audio then plays |
| Mic capture fails in browser | requires `https://` or `localhost`; grant mic permission |

## Observability

Currently structured stdout logging. Planned metrics (see `architecture.md`):
first-chunk latency, real-time factor, error rate — keyed by `job_id` / `session_id`.
