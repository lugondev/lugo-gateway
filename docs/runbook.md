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
Set `VOSK_MODEL_PATH` to use a different model.

### Apple-GPU STT (whisper_mlx, ~7x faster)

```bash
.venv/bin/pip install -e ".[mlx]"     # Apple Silicon only
./scripts/convert_phowhisper_mlx.sh   # builds models/stt/phowhisper-medium-mlx
```
Then set `CONVERSATION_STT_ENGINE=whisper_mlx` (or pick it in the UI). The engine
auto-hides on non-Mac hosts, so callers fall back to the CPU `whisper` engine.

### Conversation LLM (local Ollama or online)

Local: run Ollama, set `CONVERSATION_LLM_BASE_URL=http://localhost:11434/v1` +
`CONVERSATION_LLM_MODEL=gemma2:9b`; manage/activate models in the System tab.
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

All settings are environment variables (or `.env`). See `.env.example` for the full list.

| Variable | Default | Purpose |
|----------|---------|---------|
| `APP_HOST` / `APP_PORT` | `0.0.0.0` / `8000` | bind address |
| `LOG_LEVEL` | `INFO` | logging level |
| `CORS_ALLOW_ORIGINS` | `*` | comma-separated origins, or `*` |
| `DEFAULT_STT_ENGINE` | `vosk` | default for `/transcribe` and WS |
| `STT_STREAM_SAMPLE_RATE` | `16000` | streaming audio contract rate (Hz) |
| `VOSK_MODEL_PATH` | `models/stt/vosk-model-small-en-us-0.15` | local Vosk model dir |
| `WHISPER_LOCAL_MODEL` | `phowhisper-medium` | size (`small`/`medium`/`large-v3`) or VinAI Vietnamese fine-tune `phowhisper-{tiny,base,small,medium,large}` |
| `WHISPER_LOCAL_DEVICE` | `cpu` | `cpu` \| `cuda` |
| `WHISPER_LOCAL_COMPUTE_TYPE` | `int8` | quantization |
| `WHISPER_BEAM_SIZE` | `1` | beam search width (1 = greedy; ~17% faster, no measured accuracy loss) |
| `WHISPER_CONDITION_ON_PREVIOUS_TEXT` | `false` | off avoids hallucination drift across silent gaps |
| `WHISPER_INITIAL_PROMPT` | — | seed text to bias Vietnamese orthography (empty = off) |
| `WHISPER_MLX_MODEL_PATH` | `models/stt/phowhisper-medium-mlx` | MLX model dir for the `whisper_mlx` engine (Apple-GPU, ~7x faster). Build with `scripts/convert_phowhisper_mlx.sh`; engine auto-hides if `mlx-whisper`/model absent |
| `WHISPER_SERVICE_BASE_URL` / `_API_KEY` / `_MODEL` | — | remote OpenAI-compatible STT |
| `EVENTLAB_BASE_URL` / `_API_KEY` / `_MODEL` | — | second remote STT provider |
| `REMOTE_STT_TIMEOUT_SECONDS` | `60` | remote request timeout |
| `OMNIVOICE_PATH` | `/Users/lugon/code/OmniVoice` | OmniVoice source checkout |
| `OMNIVOICE_MODEL_ID` | `k2-fsa/OmniVoice` | HF model id |
| `OMNIVOICE_DEVICE` | _(auto)_ | `cuda:0` \| `mps` \| `cpu` |
| `OMNIVOICE_DTYPE` | `float16` | torch dtype |
| `ARTIFACTS_DIR` | `artifacts` | where generated WAVs are written |
| `CONVERSATION_STT_ENGINE` | `whisper` | STT engine for the voice loop (`whisper_mlx`/…) |
| `CONVERSATION_TTS_ENGINE` | `vieneu` | TTS engine for the voice loop |
| `CONVERSATION_LLM_BASE_URL` / `_API_KEY` / `_MODEL` | — / — / `gpt-3.5-turbo` | OpenAI-compatible chat endpoint (Ollama / online); empty base url → echo responder |

## Enabling real TTS

1. Ensure the OmniVoice checkout at `OMNIVOICE_PATH` is importable and its dependencies
   (torch, etc.) are installed in the active environment.
2. Pick a device via `OMNIVOICE_DEVICE` (Apple Silicon: `mps`; NVIDIA: `cuda:0`).

Every request runs real synthesis. If the model fails to load, the provider returns
a `ProviderError` (502) instead of placeholder audio.

## VAD backends (STT preprocessing)

`STT_VAD_BACKEND` selects how voice activity is detected for batch transcription
(`/v1/stt/transcribe`, also overridable per request via the `vad_backend` form field).
All backends run **locally** — none call an external service at inference time.

| Backend | Install | Token | Notes |
|---------|---------|:-----:|-------|
| `energy` | built-in (numpy) | — | Always available; simple RMS gate. |
| `silero` | `pip install silero-vad` (pulls `torch`) | no | Neural VAD; weights auto-download (ungated). |
| `pyannote` | `pip install pyannote.audio` | **yes** | Neural VAD; weights are **gated** on Hugging Face. |

### Why pyannote needs a token (it is not a remote service)

pyannote.audio runs entirely on your machine. The token is **only used once to download
the model weights** from the Hugging Face Hub, because `pyannote/segmentation-3.0`
(`PYANNOTE_VAD_MODEL`) is a **gated repository** (HF requires you to accept the model
license before downloading). After the weights are cached locally, inference is fully
offline — no per-request API call. The VAD pipeline is built from this segmentation
model (`VoiceActivityDetection`), not the legacy `pyannote/voice-activity-detection`
pipeline (incompatible with pyannote.audio 4.x).

To enable pyannote:
1. `pip install pyannote.audio` (pulls `torch`).
2. Create a token at <https://huggingface.co/settings/tokens>.
3. Click **Agree** on `pyannote/segmentation-3.0`.
4. Set `PYANNOTE_AUTH_TOKEN=hf_...`.

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
