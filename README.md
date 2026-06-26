# speech-text-transformer

Foundation project for Speech-to-Text (Vosk/Whisper) and Text-to-Speech (OmniVoice) with REST, WebSocket, and SSE.

## Quick start

### Local Python

```bash
cd /Users/lugon/code/speech-text-transformer
python -m venv .venv
source .venv/bin/activate
pip install -e .
PYTHONPATH=apps/api_gateway uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

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
- WS /v1/stt/stream
- POST /v1/tts/synthesize
- POST /v1/tts/stream
- WS /v1/conversation/stream (voice turn-taking)
- GET /v1/events/jobs/{job_id} (SSE)
- GET /v1/events/sessions/{session_id} (SSE)
- GET /v1/system/status
- GET /v1/models
- POST /v1/models/vosk/download
- DELETE /v1/models/vosk/{name}
- GET /artifacts/{file} (generated audio)

UI playground is available at /ui and uses the same API host. It includes a system
status panel, a Vosk model manager (download/delete with progress), microphone recording
for batch STT, live WebSocket streaming, and progressive TTS playback.

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
- whisper or whisper_local: Local faster-whisper model.
- whisper_service: Remote OpenAI-compatible Whisper endpoint.
- eventlab: Remote provider using the same OpenAI-compatible transcription API.

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
