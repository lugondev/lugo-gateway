# Architecture

## Overview

A single FastAPI gateway exposes REST, WebSocket, and SSE for STT and TTS. Engines
sit behind provider interfaces so they can be swapped or added without touching the
routes. An in-memory event bus fans streaming events out to SSE subscribers.

```
                ┌────────────────────────── FastAPI gateway ───────────────────────────┐
  client ──►   │  /v1/stt/*      /v1/tts/*      /v1/events/*   /v1/conversation/*  /ui  │
                │      │              │               │              │                  │
                │      ▼              ▼               ▼              ▼                  │
                │  STTService     TTSService      EventBus     ConversationSession      │
                │   providers      providers    (replay+close)  (binary WAV/Opus out)   │
                │  ┌─────────┐   ┌────────────┐                                        │
                │  │ vosk    │   │ omnivoice  │  segmenter → per-sentence synth →       │
                │  │ whisper │   └────────────┘  audio_start / binary frame / audio_end │
                │  │ remote  │                                                          │
                │  └─────────┘                                                          │
                └──────────────────────────────────────────────────────────────────────┘
```

## Components

### API layer (`app/api/routes`)
- `health` — liveness.
- `stt` — batch `POST /transcribe`, engine list, and the streaming `WS /stream`.
- `tts` — `POST /synthesize` (returns raw audio bytes) and `POST /reference-audio`
  (voice-clone reference upload).
- `events` — SSE for STT session channels (`GET /sessions/{session_id}`).
- `conversation` — `WS /stream`, the voice turn-taking gateway; pushes reply audio
  as binary WebSocket frames (WAV/MP3 or Opus), never a URL.
- `ui` — serves the static playground.

Routes are thin: they validate input, resolve a provider, and translate domain
errors. They never embed model logic.

### Services (`app/services`)
- **STTService / TTSService** — registries mapping an engine name to a provider.
  Unknown names raise `EngineNotFoundError` (→ HTTP 400 / WS `error` event).
- **STT providers** implement `transcribe_bytes()` and optionally `open_stream()`.
  - `VoskProvider` — local, CPU-friendly, **native incremental** streaming.
  - `WhisperProvider` — local faster-whisper, defaults to large-v3-turbo.
  - `WhisperMlxProvider` (`whisper_mlx`) — Whisper on the Apple GPU via mlx-whisper.
  - `WhisperGemmaProvider` — Whisper transcript refined by the conversation LLM.
  - `RemoteWhisperProvider` — OpenAI-compatible `/audio/transcriptions` endpoint.
  - MLX engines auto-hide off Apple Silicon → callers fall back to `whisper`.
- **Conversation** (`app/services/conversation`, `routes/conversation.py`) — VAD
  endpointer + responder (echo / OpenAI-compatible LLM), streamed per-sentence to TTS
  with barge-in. Audio in as PCM16 or Opus (`core/opus.py`).
- **Model managers** (`whisper_models`, `llm_models`, `tts_models`, `models`) —
  download / select / delete weights for the System-tab managers.
- **TTS providers** implement `synthesize()`.
  - `OmniVoiceProvider` — lazy-loads OmniVoice from the configured path (admin System
    tab), runs inference in a worker thread, and raises `ProviderError` (502) on failure.
- **ArtifactStore** — persists voice-clone **reference audio** only
  (`POST /v1/tts/reference-audio`, `ref_audio_path`). Synthesized reply audio is never
  written here or anywhere else on disk; providers return bytes directly to the
  caller (HTTP response or WebSocket frame). The directory is not mounted over HTTP.
- **segmenter** — splits text into sentence-sized chunks for streaming TTS.

### Streaming (`app/streaming/event_bus.py`)
`InMemoryEventBus` provides pub/sub with two properties that matter for correctness:
- **Replay** — a bounded per-channel history is replayed to late subscribers, so the
  SSE client never misses `session_started`/early events due to the
  subscribe-after-publish race.
- **Terminal close** — a `done` event closes the channel and wakes subscribers with a
  sentinel so the SSE generator stops and memory is reclaimed.

Only STT streaming publishes to this bus today (`session:{id}` channels); TTS/
conversation audio goes straight to the WebSocket and has no SSE/event-bus path.

## Data flows

### STT streaming (WebSocket)
1. Client connects to `/v1/stt/stream` and streams raw PCM16 mono frames.
2. The route opens a per-connection `STTStream` from the provider.
3. Vosk emits `partial`/`final` as audio arrives; buffering engines emit one `final`
   on `flush`/`end`.
4. Events go to the socket and are mirrored to `session:{id}` on the event bus.

### Conversation reply audio (WebSocket, binary frames)
1. The conversation session segments the reply text into sentences and synthesizes
   each one.
2. For each sentence the server sends `audio_start` (JSON: `turn`, `chunk_index`,
   `text?`, `codec`), then **one binary frame** carrying the complete audio
   container for that sentence, then `audio_end` (JSON: `turn`, `chunk_index`).
3. `codec` is `"wav"` or `"mp3"` (default, `?audio_out=wav`, mapped from the TTS
   provider's media type) or `"opus"` (`?audio_out=opus`, for ESP32/RPi — framed as
   many small Opus packets instead of one container). Nothing is persisted to disk
   and no URL is ever sent — see `docs/api.md`.

OmniVoice generates per segment (no native token-level streaming), so first-byte time
is driven by chunk size — short leading sentences play sooner.

## The audio contract
Streaming STT is fixed to **PCM signed-16, mono**, at the configured stream sample
rate (admin System tab; 16 kHz default). Clients resample at the edge (the browser
playground decimates the mic's native rate down to 16 kHz before sending). OmniVoice
output is 24 kHz WAV.

## Upgrade paths (not yet implemented)
- **Event bus → Redis Pub/Sub + streams** for multi-worker scale and reconnect/replay
  across processes. The `InMemoryEventBus` interface is the seam.
- **Reliability** — queue, retries, timeouts, circuit breakers around model calls.
- **Observability** — first-chunk latency, real-time factor, error-rate metrics, and
  structured tracing keyed by `session_id`.
- **Auth** at the gateway.
