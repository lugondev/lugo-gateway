# Runbook

## Run locally

```bash
cd /Users/lugon/code/speech-text-transformer
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # then edit as needed
PYTHONPATH=apps/api_gateway uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open the playground at `http://localhost:8000/ui`. Interactive API docs are at `/docs`.

> Run uvicorn from the repo root: the static mount and `ARTIFACTS_DIR` resolve relative
> to the working directory.

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
./scripts/convert_phowhisper_mlx.sh   # builds models/stt/phowhisper-medium-mlx
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

Brings up the API (port 8000) and a Redis container (reserved for the future bus
upgrade; the current in-memory bus does not require it).

## Tests

```bash
pip install -e ".[dev]"
pytest          # 25 tests: event bus, segmenter, audio, errors, STT WS, TTS SSE e2e
ruff check apps tests
```

## Configuration

Bootstrap settings (process identity, networking, auth, storage paths) are environment
variables (or `.env`). See `.env.example` for the full list.

| Variable | Default | Purpose |
|----------|---------|---------|
| `APP_HOST` / `APP_PORT` | `0.0.0.0` / `8000` | bind address |
| `LOG_LEVEL` | `INFO` | logging level |
| `CORS_ALLOW_ORIGINS` | `*` | comma-separated origins, or `*` |
| `ADMIN_PASSWORD` | — | browser control-panel login |
| `ARTIFACTS_DIR` | `artifacts` | where generated WAVs are written |

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
(the default pyannote VAD model, configurable in the System tab) is a **gated
repository** (HF requires you to accept the model license before downloading). After
the weights are cached locally, inference is fully offline — no per-request API call.
The VAD pipeline is built from this segmentation model (`VoiceActivityDetection`), not
the legacy `pyannote/voice-activity-detection` pipeline (incompatible with
pyannote.audio 4.x).

To enable pyannote:
1. `pip install pyannote.audio` (pulls `torch`).
2. Create a token at <https://huggingface.co/settings/tokens>.
3. Click **Agree** on `pyannote/segmentation-3.0`.
4. Set the pyannote auth token in the admin System tab (preprocessing group).

`silero` needs no token and is the recommended neural VAD when you don't want to deal with
HF gating. If a selected backend is unavailable it falls back to `energy`.

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| WS `error`: "Vosk model not found" | run `scripts/download_vosk_model.sh` |
| `/transcribe` 400 "requires WAV PCM16 mono" | convert input, e.g. `ffmpeg -i in.mp3 -ar 16000 -ac 1 -c:a pcm_s16le out.wav` |
| TTS request fails with 502 | engine failed to load/run — check logs for the underlying error |
| SSE never closes | fixed — streams close on the terminal `done` event |
| Browser autoplay blocked | click a control once; chunk audio then plays |
| Mic capture fails in browser | requires `https://` or `localhost`; grant mic permission |

## Observability

Currently structured stdout logging. Planned metrics (see `architecture.md`):
first-chunk latency, real-time factor, error rate — keyed by `job_id` / `session_id`.
