# speech-text-transformer

Local gateway unifying Speech-to-Text, Text-to-Speech, and a voice Conversation loop
over REST / WebSocket / SSE, with a browser playground. STT: Vosk, faster-whisper
(PhoWhisper for Vietnamese), Apple-GPU MLX (`whisper_mlx`, `qwen_omni`), remote
Whisper. TTS: OmniVoice, VieNeu, and more. Conversation: VAD turn-taking + barge-in,
local/online LLM or audio-native Qwen3-Omni, PCM or Opus transport.

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

Optional extras: `pip install -e ".[mlx]"` (Apple-Silicon GPU STT: `whisper_mlx`,
`qwen_omni`), `pip install -e ".[opus]"` (Opus audio transport — also needs system
libopus: `brew install opus` / `apt install libopus0`).

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

- [docs/api.md](docs/api.md) — full REST / WebSocket / SSE reference and schemas.
- [docs/architecture.md](docs/architecture.md) — components, data flows, upgrade paths.
- [docs/runbook.md](docs/runbook.md) — run, configure, troubleshoot.

## Endpoints

- GET /health
- GET /ui
- GET /v1/stt/engines
- POST /v1/stt/transcribe
- POST /v1/stt/warm (preload a heavy STT model, e.g. qwen_omni)
- WS /v1/stt/stream
- POST /v1/tts/synthesize
- POST /v1/tts/stream
- WS /v1/conversation/stream (voice turn-taking; `?audio_codec=pcm16|opus`)
- GET/POST /v1/conversation/llm + POST /v1/conversation/llm/reset (online LLM config)
- POST /v1/conversation/chat (text chat with the conversation responder)
- POST /v1/models/{whisper,qwen-omni,llm,...}/download|select|delete
- GET /v1/events/jobs/{job_id} (SSE)
- GET /v1/events/sessions/{session_id} (SSE)
- GET /v1/system/status
- GET /v1/models
- POST /v1/models/vosk/download
- DELETE /v1/models/vosk/{name}
- GET /artifacts/{file} (generated audio)

UI playground at `/ui` (same API host), tabbed: **System** (status, model managers for
Vosk/Whisper/TTS/Qwen-Omni/LLM, VAD+denoise config, online-LLM provider), **Speech →
Text** (mic record + streaming), **Text → Speech**, **Conversation** (live voice loop,
shows the active STT/LLM/TTS), and **LLM Chat**.

## Streaming protocols

### STT WebSocket (`/v1/stt/stream`)

Audio contract: raw PCM signed-16, mono, at `STT_STREAM_SAMPLE_RATE` (default 16 kHz).
Query params: `?engine=vosk&language=en&sample_rate=16000`.

- Client -> server: binary PCM frames, or text control `{"type":"flush"}` / `{"type":"end"}`.
- Server -> client events: `session_started`, `partial`, `final`, `error`, `done`.
- Vosk decodes incrementally (real partials + finals). Other engines buffer audio and
  return a single `final` on flush/end.

### TTS pseudo-streaming (SSE)

1. POST `/v1/tts/stream` with `{text, engine}` -> `{job_id}`.
2. GET `/v1/events/jobs/{job_id}` (SSE). The event bus buffers events, so subscribing
   after the job starts still replays `queued` and earlier chunks.
3. Events: `queued`, `audio_chunk` (text split into sentences; each chunk carries an
   `audio_url`), `error`, `done`. The stream closes itself on `done`.

When `ENABLE_MOCK_ENGINES=true` (default), TTS returns silent placeholder WAVs so the
full pipeline runs without loading OmniVoice. Set it `false` for real OmniVoice inference.

## STT engine options

- vosk: Local Vosk model.
- whisper or whisper_local: Local faster-whisper. Defaults to **PhoWhisper** (VinAI
  Vietnamese fine-tune, `phowhisper-medium`) — far better Vietnamese tones/diacritics
  than vanilla Whisper. CPU on macOS (~3.7s/utterance).
- **whisper_mlx**: PhoWhisper on the Apple-Silicon **GPU** via MLX — ~0.5s/utterance
  (~7× faster than CPU), same accuracy. Mac only; auto-falls back to `whisper`
  elsewhere. Build the model with `scripts/convert_phowhisper_mlx.sh`.
- **qwen_omni**: audio-native **Qwen3-Omni** (MLX, Apple GPU). Transcribes with
  punctuation/casing and, in conversation, can answer the audio directly (see below).
  Heavy 30B model (~1s/utterance); download a quant in the System tab.
- whisper_gemma: faster-whisper transcript refined by the conversation LLM (Gemma) —
  fixes spelling/diacritics/punctuation. Falls back to raw Whisper text if no LLM.
- whisper_service: Remote OpenAI-compatible Whisper endpoint.
- eventlab: Remote provider using the same OpenAI-compatible transcription API.

## Conversation (voice)

`WS /v1/conversation/stream` runs a full voice loop: VAD endpointing → STT → reply →
streamed TTS, with barge-in. The reply comes from:

- **A text LLM** (cascade): local **Ollama** (default `gemma2:9b`), or any
  OpenAI-compatible **online** provider (OpenAI/Groq/Together) configured at runtime
  via the System tab or `POST /v1/conversation/llm` — held in memory only.
- **Audio-native**: when STT is `qwen_omni`, Qwen3-Omni answers the audio itself
  (no separate text LLM), toggled by `CONVERSATION_AUDIO_NATIVE` (default on).

It's a unified **text/audio → text/audio** gateway: input is audio frames or a
`{"type":"text"}` message; `?output=audio,text` picks what comes back — covering
audio→audio, text→audio, audio→text, text→text. Input audio is PCM16 or Opus
(`?audio_codec=opus`); reply audio is an `audio_url` (browser) or pushed Opus binary
frames (`?audio_out=opus`, for ESP32 / Raspberry Pi). See [docs/api.md](docs/api.md).

Remote engine endpoints are configured in .env:

- WHISPER_SERVICE_BASE_URL, WHISPER_SERVICE_API_KEY, WHISPER_SERVICE_MODEL
- EVENTLAB_BASE_URL, EVENTLAB_API_KEY, EVENTLAB_MODEL

Expected remote API format:

- POST {base_url}/audio/transcriptions
- multipart file field: file
- form fields: model, language(optional), response_format=json
- response json includes text

## Tests

```bash
pip install -e ".[dev]"
pytest
```

## Notes

- OmniVoice source path is configured via OMNIVOICE_PATH.
- Vosk requires WAV PCM16 mono input for local transcribe endpoint.
