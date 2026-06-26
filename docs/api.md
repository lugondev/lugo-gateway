# API Reference (v1)

Base URL (local): `http://localhost:8000`

All JSON responses use a common envelope:

```json
{ "success": true, "data": { ... }, "error": null }
```

On a handled domain error the envelope is `{ "success": false, "error": "<message>" }`
with an appropriate HTTP status (e.g. `400` for an unknown engine).

---

## Health & meta

### `GET /health`
Liveness probe. → `{ "status": "ok" }`

### `GET /`
Service banner. → `{ "service": "...", "env": "dev" }`

### `GET /ui`
Browser playground (static HTML/JS at `/static`).

---

## STT — Speech to Text

### `GET /v1/stt/engines`
List configured engines.

```json
{
  "success": true,
  "data": [
    { "engine": "vosk", "mode": "local", "available": true, "configured": true, "detail": "vosk-model-small-en-us-0.15" },
    { "engine": "whisper", "mode": "local", "available": true, "configured": true, "detail": "small · cached" },
    { "engine": "whisper_service", "mode": "remote", "available": false, "configured": false, "detail": null }
  ]
}
```

`available` reflects whether the engine is usable now: Vosk needs its model on disk,
whisper needs faster-whisper installed, remote engines need a base URL. `detail` is the
specific model/version (Vosk model dir, active whisper size, or remote model id). Clients
should list only `available` engines.

### `POST /v1/stt/transcribe`
Batch transcription. `multipart/form-data`:

| field | type | notes |
|-------|------|-------|
| `audio` | file | WAV PCM16 mono required for `vosk`; whisper accepts common formats |
| `engine` | string | `vosk` \| `whisper` \| `whisper_local` \| `whisper_service` \| `eventlab` |
| `language` | string? | optional hint, e.g. `en`, `vi` |
| `denoise` | bool? | spectral noise reduction (default `STT_NOISE_REDUCE_ENABLED`) |
| `vad` | bool? | energy VAD gate (default `STT_VAD_ENABLED`) |

Preprocessing (`denoise`/`vad`) applies to mono PCM16 WAV input; other formats pass
through. `vad` also drives faster-whisper's internal `vad_filter`.

Response `data` is an `STTResult`:

```json
{ "engine": "vosk", "text": "hello world", "is_final": true, "confidence": null }
```

Errors: invalid audio / missing model → `400` with a descriptive message.

### `WS /v1/stt/stream`
Real-time transcription. Connect with query params:

```
ws://localhost:8000/v1/stt/stream?engine=vosk&language=en&sample_rate=16000&denoise=false&vad=true
```

`denoise` and `vad` toggle per-frame noise reduction / VAD gating (defaults from settings).

**Audio contract:** raw PCM signed-16, mono, at `sample_rate` (default
`STT_STREAM_SAMPLE_RATE`, 16 kHz).

Client → server:
- Binary frames: raw PCM chunks.
- Text control: `{"type":"flush"}` (emit a final for buffered audio) or
  `{"type":"end"}` (finalize, emit `done`, close).

Server → client (JSON `StreamEvent`):

| `event_type` | when | payload |
|--------------|------|---------|
| `session_started` | on connect | `session_id`, `sample_rate` |
| `partial` | interim hypothesis (Vosk only) | `STTResult` (`is_final:false`) |
| `final` | utterance/segment finalized | `STTResult` (`is_final:true`) |
| `error` | bad engine / missing model / decode error | `{ "message": "..." }` |
| `done` | stream ended | `{ "message": "stream ended" }` |

Engine behavior:
- **Vosk** decodes incrementally → real `partial` then `final` per utterance.
- **Whisper / remote** buffer all audio and return a single `final` on flush/end.

Events are also mirrored to the SSE channel `GET /v1/events/sessions/{session_id}`.

---

## Conversation (voice turn-taking)

### `WS /v1/conversation/stream`
Full-duplex voice loop: stream mic audio, the server endpoints each turn with VAD,
transcribes it, generates a reply, and streams TTS audio back.

```
ws://localhost:8000/v1/conversation/stream?stt_engine=vosk&tts_engine=vieneu&voice=Ngọc Lan&sample_rate=16000
```

Client → server: binary PCM16 mono frames; text control `{"type":"reset"}` (clear
history) / `{"type":"end"}` (finalize + close).

Server → client events (`{"event": ...}`):

| `event` | when | key fields |
|---------|------|-----------|
| `session_started` | on connect | `stt_engine`, `tts_engine`, `responder` |
| `speech_start` | user starts speaking | — |
| `speech_end` | VAD detects end of turn | `speech_ms` |
| `processing` | transcribing + generating | `turn` |
| `user_transcript` | STT result for the turn | `text` |
| `response_text` | assistant reply text | `text`, `responder` |
| `audio_chunk` | reply TTS, one per sentence | `chunk_index`, `text`, `audio_url` |
| `turn_done` | turn complete | `turn` |
| `error` / `done` / `reset` | — | — |

Turn-taking uses an energy VAD endpointer (`CONVERSATION_*` settings). Replies come
from a built-in echo responder, or an OpenAI-compatible chat endpoint when
`CONVERSATION_LLM_BASE_URL` is set. Long replies are sentence-split and synthesized
chunk-by-chunk so playback starts early.

---

## TTS — Text to Speech

### `GET /v1/tts/engines`
Lists TTS engines with `available`, `detail` (model/version), `mock`, `default`.
Engines: `omnivoice` (24 kHz, multilingual, run via its own venv subprocess) and
`vieneu` (VieNeu-TTS v3 turbo, 48 kHz, Vietnamese, `pip install vieneu`).

### `GET /v1/tts/voices?engine=vieneu`
Lists VieNeu preset voices `[{ "label", "voice" }]`.

### `POST /v1/tts/synthesize`
Batch synthesis. JSON body (`TTSRequest`):

```json
{
  "text": "Hello world",
  "engine": "omnivoice",
  "language": null,
  "speed": null,
  "instruct": null,
  "ref_audio_path": null,
  "ref_text": null
}
```

Voice modes (OmniVoice):
- **Clone** — provide `ref_audio_path` (+ optional `ref_text`).
- **Design** — provide `instruct`, e.g. `"female, low pitch, british accent"`.
- **Auto** — provide neither.

Response `data` (`TTSResult`):

```json
{
  "engine": "omnivoice",
  "sample_rate": 24000,
  "audio_url": "/artifacts/<id>.wav",
  "duration_seconds": 1.6,
  "job_id": null,
  "text": "Hello world",
  "mock": true
}
```

`mock: true` means a silent placeholder was returned (see `ENABLE_MOCK_ENGINES`).

### `POST /v1/tts/stream`
Start a pseudo-streaming synthesis job. Same body as `synthesize`.
→ `{ "data": { "job_id": "<uuid>" } }`. Subscribe via SSE to receive chunks.

---

## Events (SSE)

### `GET /v1/events/jobs/{job_id}`
### `GET /v1/events/sessions/{session_id}`

Server-Sent Events stream. Each message is `event: <type>` + `data: <StreamEvent JSON>`.

The bus **buffers** events per channel, so subscribing slightly after the producer
starts still replays earlier events (e.g. `queued`). The stream **closes itself**
after a terminal `done` event.

TTS job event sequence:

| `event_type` | payload |
|--------------|---------|
| `queued` | `{ "text", "total_chunks" }` |
| `audio_chunk` | `{ "chunk_index", "text", "audio_url", "duration_seconds", "mock" }` |
| `error` | `{ "message" }` (only on failure) |
| `done` | `{ "message" }` |

---

## System & Models

### `GET /v1/system/status`
Aggregated runtime status: app env, STT engines (+ remote `configured`), TTS mock flag
and OmniVoice presence, whisper-local cache state, active Vosk model + installed Vosk
models, and artifact count/size.

### `GET /v1/models`
Vosk and Whisper model catalogs and state:

```json
{
  "vosk": {
    "installed": [{ "name": "vosk-model-small-en-us-0.15", "size_bytes": 70898967, "path": "..." }],
    "suggestions": [{ "name": "vosk-model-small-vn-0.4", "label": "Vietnamese (small)", "installed": false }],
    "jobs": { "vosk-model-small-vn-0.4": { "state": "downloading", "progress": 0.42, "error": null } },
    "base_dir": "models/stt"
  },
  "whisper": {
    "active": "small",
    "models": [
      { "size": "small", "label": "Small (default)", "cached": true, "active": true, "size_bytes": 503000000, "job": null }
    ]
  }
}
```

Vosk `jobs[name].state` is `downloading` \| `installed` \| `error` (with `progress` 0–1).
Whisper `job.state` is `downloading` \| `installed` \| `error` (progress is indeterminate).
Poll this endpoint while a download is active.

### Vosk
- `POST /v1/models/vosk/download` — body `{ "name": "vosk-model-small-vn-0.4" }`. Downloads
  `{VOSK_MODEL_BASE_URL}/{name}.zip` in the background, extracts into `STT_MODEL_DIR`.
  Invalid names → `400`; a missing model surfaces as a job `error` (HTTP 404).
- `DELETE /v1/models/vosk/{name}` — removes an installed model dir (traversal-protected);
  not installed → `400`.

### Whisper (faster-whisper)
- `POST /v1/models/whisper/download` — body `{ "size": "tiny" }`. Warms the size (fetches
  weights into the Hugging Face cache) in the background.
- `DELETE /v1/models/whisper/{size}` — removes the size's hub cache dir; not cached → `400`.
- `POST /v1/models/whisper/select` — body `{ "size": "medium" }`. Switches the active
  local-whisper model at runtime (not persisted across restarts).

> Browse the full Vosk catalog at <https://alphacephei.com/vosk/models>; any name can be
> downloaded. Whisper sizes: `tiny`, `base`, `small`, `medium`, `large-v3`.

---

## Artifacts

### `GET /artifacts/{file}`
Serves generated audio WAV files referenced by `audio_url`. Backed by the local
filesystem (`ARTIFACTS_DIR`); swap for object storage in production.

---

## StreamEvent schema

```json
{
  "event_type": "audio_chunk",
  "session_id": null,
  "job_id": "…",
  "sequence": 2,
  "timestamp": "2026-06-25T15:10:57.499171Z",
  "payload": { }
}
```

`timestamp` is timezone-aware UTC (ISO 8601). `sequence` is monotonic per stream.
