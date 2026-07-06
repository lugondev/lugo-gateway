# Architecture

## Overview

A single FastAPI gateway exposes REST, WebSocket, and SSE for STT and TTS. Engines
sit behind provider interfaces so they can be swapped or added without touching the
routes. An in-memory event bus fans streaming events out to SSE subscribers.

```
                ┌─────────────────────────── FastAPI gateway ───────────────────────────┐
  client ──►   │  /v1/stt/*      /v1/tts/*       /v1/events/*        /artifacts/*  /ui   │
                │      │              │                │                  │               │
                │      ▼              ▼                ▼                  ▼               │
                │  STTService     TTSService       EventBus         ArtifactStore        │
                │   providers      providers     (replay+close)     (local FS → S3)      │
                │  ┌─────────┐   ┌────────────┐                                          │
                │  │ vosk    │   │ omnivoice  │  segmenter → per-chunk synth → events    │
                │  │ whisper │   └────────────┘                                          │
                │  │ remote  │                                                            │
                │  └─────────┘                                                            │
                └────────────────────────────────────────────────────────────────────────┘
```

## Components

### API layer (`app/api/routes`)
- `health` — liveness.
- `stt` — batch `POST /transcribe`, engine list, and the streaming `WS /stream`.
- `tts` — `POST /synthesize` and `POST /stream` (spawns a background job).
- `events` — SSE for job/session channels.
- `ui` — serves the static playground.

Routes are thin: they validate input, resolve a provider, and translate domain
errors. They never embed model logic.

### Services (`app/services`)
- **STTService / TTSService** — registries mapping an engine name to a provider.
  Unknown names raise `EngineNotFoundError` (→ HTTP 400 / WS `error` event).
- **STT providers** implement `transcribe_bytes()` and optionally `open_stream()`.
  - `VoskProvider` — local, CPU-friendly, **native incremental** streaming.
  - `WhisperProvider` — local faster-whisper, defaults to PhoWhisper (Vietnamese).
  - `WhisperMlxProvider` (`whisper_mlx`) — PhoWhisper on the Apple GPU via mlx-whisper.
  - `WhisperGemmaProvider` — Whisper transcript refined by the conversation LLM.
  - `RemoteWhisperProvider` — OpenAI-compatible `/audio/transcriptions` endpoint.
  - MLX engines auto-hide off Apple Silicon → callers fall back to `whisper`.
- **Conversation** (`app/services/conversation`, `routes/conversation.py`) — VAD
  endpointer + responder (echo / OpenAI-compatible LLM), streamed per-sentence to TTS
  with barge-in. Audio in as PCM16 or Opus (`core/opus.py`).
- **Model managers** (`whisper_models`, `llm_models`, `tts_models`, `models`) —
  download / select / delete weights for the System-tab managers.
- **TTS providers** implement `synthesize()`.
  - `OmniVoiceProvider` — lazy-loads OmniVoice from `OMNIVOICE_PATH`, runs inference
    in a worker thread, and raises `ProviderError` (502) on failure.
- **ArtifactStore** — persists generated WAVs and returns a `/artifacts/...` URL.
- **segmenter** — splits text into sentence-sized chunks for streaming TTS.

### Streaming (`app/streaming/event_bus.py`)
`InMemoryEventBus` provides pub/sub with two properties that matter for correctness:
- **Replay** — a bounded per-channel history is replayed to late subscribers, so the
  SSE client never misses `queued`/early chunks due to the subscribe-after-publish race.
- **Terminal close** — a `done` event closes the channel and wakes subscribers with a
  sentinel so the SSE generator stops and memory is reclaimed.

## Data flows

### STT streaming (WebSocket)
1. Client connects to `/v1/stt/stream` and streams raw PCM16 mono frames.
2. The route opens a per-connection `STTStream` from the provider.
3. Vosk emits `partial`/`final` as audio arrives; buffering engines emit one `final`
   on `flush`/`end`.
4. Events go to the socket and are mirrored to `session:{id}` on the event bus.

### TTS pseudo-streaming (SSE)
1. `POST /v1/tts/stream` returns a `job_id` and starts a background task.
2. The task segments the text, synthesizes each chunk, and publishes `audio_chunk`
   events carrying an `audio_url`.
3. The client subscribes to `/v1/events/jobs/{job_id}` and plays chunks in order.
4. A final `done` event closes the channel.

OmniVoice generates per segment (no native token-level streaming), so first-byte time
is driven by chunk size — short leading sentences play sooner.

## The audio contract
Streaming STT is fixed to **PCM signed-16, mono**, at `STT_STREAM_SAMPLE_RATE`
(16 kHz default). Clients resample at the edge (the browser playground decimates the
mic's native rate down to 16 kHz before sending). OmniVoice output is 24 kHz WAV.

## Upgrade paths (not yet implemented)
- **Event bus → Redis Pub/Sub + streams** for multi-worker scale and reconnect/replay
  across processes. The `InMemoryEventBus` interface is the seam.
- **ArtifactStore → S3-compatible object storage**; callers already depend only on the
  returned URL.
- **Reliability** — queue, retries, timeouts, circuit breakers around model calls.
- **Observability** — first-chunk latency, real-time factor, error-rate metrics, and
  structured tracing keyed by `job_id`/`session_id`.
- **Auth** at the gateway.
