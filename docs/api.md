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
List configured engines and their availability.

```json
{
  "success": true,
  "data": [
    { "engine": "vosk", "mode": "local", "available": true, "configured": true, "detail": "vosk-model-small-en-us-0.15" },
    { "engine": "whisper", "mode": "local", "available": true, "configured": true, "detail": "small · cached" },
    { "engine": "whisper_service", "mode": "remote", "available": true, "configured": true, "detail": "whisper-1" }
  ]
}
```

`available` reflects whether the engine is usable now: Vosk needs its model on disk,
whisper needs faster-whisper installed, `whisper_mlx` needs mlx + a built model
(Apple Silicon), remote engines need a Model Registry entry with valid credentials.
`detail` is the specific model/version. Clients should list only `available` engines.

**Remote STT configuration:** To enable `whisper_service` or `eventlab`, create a Model Registry
entry via `POST /v1/model_registry` with `kind="stt"`, `engine="whisper_service"` (or
`"eventlab"`), and supply `base_url` and `api_key` (see Model Registry section).

### `POST /v1/stt/warm?engine=<engine>`
Preload a heavy model into memory (~10–20s the first time; cached after). The UI
calls this before the first conversation turn so it isn't a cold wait.

### `POST /v1/stt/transcribe`
Batch transcription. `multipart/form-data`:

| field | type | notes |
|-------|------|-------|
| `audio` | file | WAV PCM16 mono required for `vosk`; whisper accepts common formats |
| `engine` | string | `vosk` \| `whisper` \| `whisper_local` \| `whisper_mlx` \| `whisper_service` \| `eventlab` |
| `language` | string? | optional hint, e.g. `en`, `vi` |
| `denoise` | bool? | spectral noise reduction (default from admin System settings > preprocessing) |
| `vad` | bool? | VAD gate (default from admin System settings > preprocessing) |
| `vad_backend` | string? | `energy` \| `silero` \| `pyannote` (default from admin System settings > preprocessing) — see runbook "VAD backends" |

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

**Audio contract:** raw PCM signed-16, mono, at `sample_rate` (default from the
admin System tab's configured stream sample rate, 16 kHz).

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
A unified **text/audio → text/audio** gateway (browser + IoT). Input is either audio
frames (VAD-endpointed) or a text message; output is text events and/or synthesized
audio. Supports the full matrix: audio→audio, text→audio, audio→text, text→text.

```
ws://localhost:8000/v1/conversation/stream?stt_engine=whisper_mlx&tts_engine=vieneu&sample_rate=16000&audio_codec=opus&output=audio,text&audio_out=opus&output_sample_rate=24000
```

| query param | default | meaning |
|-------------|---------|---------|
| `stt_engine` / `tts_engine` / `voice` / `language` | settings | per-session engines |
| `profile` | — | named **chatllm profile** (see below) — sets LLM model/system prompt/TTS/MCP tools/memory in one shot |
| `sample_rate` | 16000 | input audio rate (Hz) |
| `audio_codec` | `pcm16` | **input** codec: `pcm16` or `opus` |
| `output` | `audio,text` | what to send back: any of `audio`, `text` |
| `audio_out` | `url` | reply-audio delivery: `url` (browser fetches /artifacts) or `opus` (binary frames pushed — for devices) |
| `output_sample_rate` | 24000 | output Opus frame rate when `audio_out=opus` |

**`profile`** does double duty:
1. If it names a saved profile (`POST /v1/profiles`), the session uses that profile's
   `llm` (base_url/api_key/model), `system_prompt`, `tts.engine`/`tts.voice`, `mcp_servers`,
   and `memory` settings — overriding `.env` defaults. An explicit `stt_engine`/`tts_engine`/
   `voice` query param still wins over the profile's STT choice (TTS engine from the
   profile wins over `tts_engine` if the profile sets one).
2. If it matches a built-in **language preset** (`vi` / `en` / `multi` / `en_vi`), it also
   selects the STT engine + language for that language, unless `stt_engine`/`language` are
   passed explicitly. A profile can be named e.g. `vi` to get both behaviors at once.

If `profile` is set but unknown, the server replies with a `warning` event and falls back
to defaults (the connection still proceeds).

Client → server:
- binary frames — audio input (PCM16, or Opus packets when `audio_codec=opus`).
- `{"type":"text","text":"…"}` — a text-input turn (no mic).
- `{"type":"reset"}` clear history · `{"type":"abort"}` cancel turn · `{"type":"end"}` finalize+close.

**Input audio** (`audio_codec`): `pcm16` (raw 16-bit mono) or `opus` (raw packets, ~10×
less bandwidth — native for ESP32/RPi firmware + browser WebCodecs; server decodes via
libopus, falls back to `pcm16` if absent).

**Output audio** (`audio_out=opus`): each reply sentence is sent as JSON `audio_start`
`{chunk_index, text, codec:"opus", sample_rate, frames}`, then `frames` binary Opus
packets (mono @ `output_sample_rate`, 60 ms each), then `audio_end`. The packets are
**paced**: the first ~5 go out immediately (fast first audio), the rest at one 60 ms
frame apart so a small device buffer isn't flooded on long replies. With `audio_out=url`
(default) the server sends an `audio_chunk` with an `audio_url` instead. Browsers can
decode the Opus frames via WebCodecs `AudioDecoder` — see `docs/device-integration.md` §6.

Server → client events (`{"event": ...}`):

| `event` | when | key fields |
|---------|------|-----------|
| `session_started` | on connect | `stt_engine`, `stt_detail`, `tts_engine`, `tts_detail`, `responder`, `llm_model`, `audio_codec`, `output`, `audio_out`, `output_sample_rate` |
| `speech_start` | user starts speaking | — |
| `speech_end` | VAD detects end of turn | `speech_ms` |
| `processing` | transcribing + generating | `turn` |
| `user_transcript` | STT result (or echoed text input) for the turn | `text` |
| `response_text` | assistant reply text (when `output` includes `text`) | `text`, `responder` |
| `audio_chunk` | reply TTS sentence as a URL (when `audio_out=url`) | `chunk_index`, `text`, `audio_url` |
| `audio_start` / `audio_end` | brackets binary Opus frames for a sentence (when `audio_out=opus`) | `chunk_index`, `codec`, `sample_rate`, `frames` |
| `aborted` | turn cancelled (barge-in / superseded) | `reason` |
| `turn_done` | turn complete | `turn` |
| `error` / `done` / `reset` | — | — |

Turn-taking uses an energy VAD endpointer (`CONVERSATION_*` settings). Long replies
are sentence-split and synthesized chunk-by-chunk so playback starts early. A
`speech_start` mid-reply is barge-in: the in-progress turn is cancelled (`aborted`).

The reply comes from:
- **Echo** — built-in, when no LLM is configured.
- **Text LLM** (cascade) — any OpenAI-compatible chat endpoint (local Ollama or an
  online provider). `responder` = `"llm"`, `llm_model` = the active model.

### Conversation LLM config

| route | does |
|-------|------|
| `GET /v1/conversation/llm` | current config: `base_url`, `model`, `api_key_set`, `responder` |
| `POST /v1/conversation/llm` | set `{base_url, api_key, model}` at runtime (any OpenAI-compatible endpoint). API key kept in memory only — never echoed or persisted |
| `POST /v1/conversation/llm/reset` | revert to the `.env` config |
| `POST /v1/conversation/chat` | `{messages:[…]}` → text reply from the active responder. Accepts the same `?profile=` and `?session_id=` params as the WS stream |

### Profiles — named chatllm presets

A **profile** bundles everything a conversation session needs into one name: LLM
endpoint/model, system prompt, TTS engine/voice, MCP tool servers, and memory settings.
Activate one on any conversation session with `?profile=<name>` (WS stream or
`POST /v1/conversation/chat`) — see `profile` in the WS query-param table above and
[device-integration.md](device-integration.md) for ESP32/RPi usage.

| route | does |
|-------|------|
| `GET /v1/profiles` | list all profiles (`api_key` masked as `***`) |
| `POST /v1/profiles` | create/replace a profile — body: `{name, nickname, llm:{base_url,api_key,model}, system_prompt, tts:{engine,voice}, mcp_servers:[…], memory:{enabled,mode,top_k,extractor_model,embed_model}}` |
| `GET /v1/profiles/{name}` | fetch one profile |
| `PUT /v1/profiles/{name}` | update a profile (full replace) |
| `DELETE /v1/profiles/{name}` | delete a profile |

Example — create a profile for a hands-free kitchen device pointed at a local Ollama model:
```bash
curl -X POST http://localhost:8000/v1/profiles \
  -H "Content-Type: application/json" \
  -d '{
        "name": "kitchen",
        "llm": {"base_url": "http://localhost:11434/v1", "model": "qwen2.5:7b"},
        "system_prompt": "You are a concise kitchen assistant.",
        "tts": {"engine": "vieneu"}
      }'
```
Then point the device at `?profile=kitchen` instead of setting `tts_engine`/LLM config
per-request.

### `WS /v1/lugo/stream`

The **Lugo** device protocol — a connect-on-wake WebSocket for ESP32/RPi-class voice
devices (and future agent/browser clients). Unlike `/v1/conversation/stream`
(always-streaming, query-param config), a Lugo device stays disconnected while idle,
opens the socket only on a wake trigger, identifies itself with a **profile name**
instead of raw engine/voice choices, and the **server** owns the idle-disconnect
timer.

```
ws://localhost:8000/v1/lugo/stream
```

**Lifecycle:** SLEEP (no WS, radio idle) → wake trigger (button / local command —
on-device **wake word is Phase 2**) → open WS → client sends `wakeup` → server
resolves the profile and replies `welcome` → LISTENING (streaming mic, server VAD
endpoints the turn) ⇄ SPEAKING (server pushes `tts` + audio) → after
`idle_timeout_s` of inactivity the **server** sends `goodbye` and closes the socket
→ device returns to SLEEP.

**Handshake** — the first frame must be a `wakeup` text frame:
```json
{"type": "wakeup", "profile": "kitchen", "audio_params": {"format": "opus", "sample_rate": 16000, "frame_duration": 60}}
```
The server resolves LLM / TTS / system prompt / MCP tools / memory from the named
profile — the device never sends raw engine/voice choices (STT still comes from
server defaults, not the profile; see "Profiles" above) — and replies:
```json
{"type": "welcome", "session_id": "…", "transport": "websocket", "audio_params": {"sample_rate": 24000}, "idle_timeout_s": 30}
```
`idle_timeout_s` echoes the profile's `session.idle_timeout_s` (default 30; `0` =
never auto-disconnect), so the device arms its watchdog from server truth instead
of a hardcoded value. If `profile` is set but unknown, the server replies
`{"type":"error","message":"profile '<name>' not found"}` and closes the socket —
a `wakeup` always resolves or fails loudly, never a silent fallback. Any other
message, or a binary frame, as the first frame is also an `error` + close.

**Binary framing (v3):** audio travels on WebSocket binary frames wrapped in a
4-byte header — `struct { uint8 type; uint8 reserved; uint16 payload_size
(big-endian); } + payload`. `type` 0 = Opus audio; `type` 1 = JSON is reserved for
a future JSON-over-binary path (Phase 1 sends all JSON control on **text** frames
only). Reply audio (server → client) is always v3-wrapped; Opus packets from the
device on the way up are decoded directly and don't require the v3 header.

Client → server:

| `type` | payload | meaning |
|--------|---------|---------|
| `wakeup` | `{profile, audio_params:{format,sample_rate,frame_duration}}` | handshake (first frame only) |
| `text` | `{text}` | text-input turn (no mic) |
| `abort` | `{reason}` | **barge-in** — cancel the bot's in-flight turn; the connection stays open |
| `listen` | `{state, mode}` | turn/listen control; Phase 1 no-op — server VAD drives turn segmentation in `auto` mode |
| *(binary)* | Opus packets | mic audio up (v3 wrapping optional on uplink) |

Server → client:

| `type` | payload | when |
|--------|---------|------|
| `welcome` | `{session_id, transport, audio_params, idle_timeout_s}` | reply to a valid `wakeup` |
| `stt` | `{text, final}` | transcription result for the turn |
| `tts` | `{state:"start"\|"sentence_start"\|"stop", text?}` | brackets the reply; `sentence_start` carries the sentence text as it's synthesized |
| `mcp` | `{...}` | tool/command output |
| `error` | `{message}` | handshake failure or mid-session error |
| `goodbye` | `{reason:"idle_timeout"}` | server-initiated idle disconnect; the socket closes right after |
| *(binary)* | v3 `type=0` Opus packets | reply audio down |

**Barge-in:** sending `abort` while the bot is speaking cancels the in-flight turn
(stops the `tts`/audio stream) without dropping the connection — the device can
immediately start a new turn (`text` or mic audio). `abort` with no active turn is
a safe no-op.

**Idle timeout:** the server tracks last activity (speech, a turn, or audio
playing) and, once `idle_timeout_s` elapses with the connection otherwise idle,
sends `goodbye{reason:"idle_timeout"}` and closes the WebSocket. Setting a
profile's `session.idle_timeout_s` to `0` disables this (the connection is only
closed by the client or a transport drop).

**Not yet implemented (Phase 2):** on-device wake-word detection (the `wakeup`
trigger is button/local-command only in Phase 1), a live `listen{detect}` mode,
and remote-call (server-initiated wake over an always-on channel). The `listen`
message and `wakeup` shape already reserve room for these.

---

## TTS — Text to Speech

### `GET /v1/tts/engines`
List configured TTS engines.

Returns engines with `available`, `detail` (model/version), `mock`, `default` fields.
Available engines: `omnivoice` (24 kHz, multilingual, subprocess-based), `vieneu` (VieNeu-TTS v3 turbo,
48 kHz, Vietnamese), and others defined via Model Registry.

**OmniVoice configuration:** To use OmniVoice, create a Model Registry entry via `POST /v1/model_registry`
with `kind="tts"`, `engine="omnivoice"`, and optional engine-specific config in the `config` dict
(e.g. `device`, `dtype`). See Model Registry section for details.

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
  "text": "Hello world"
}
```

A failed synthesis returns an error response (502) instead of a placeholder.

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

### `GET /v1/system/config`
Fetch the system configuration (preprocessing, conversation tuning, engine defaults).

Response `data`:
```json
{
  "base_context": "...",
  "engines": {
    "default_stt_engine": "vosk",
    "default_tts_engine": "omnivoice",
    "default_tts_engine_voice": "",
    "extra_warmup_stt_engines": "",
    "extra_warmup_tts_engines": "",
    "warmup_on_startup": true,
    "warmup_startup_timeout_s": 180,
    "ollama_bin": ""
  },
  "stt_local": {
    "stt_model_dir": "models/stt",
    "vosk_model_base_url": "https://alphacephei.com/vosk/models",
    "stt_stream_sample_rate": 16000,
    "stt_glossary_path": "",
    "stt_profile": "",
    "stt_segment_long_enabled": false,
    "stt_segment_min_seconds": 30.0,
    "stt_segment_concurrency": 4
  },
  "conversation": { ... },
  "preprocessing": { ... }
}
```

Key changes from earlier API versions:
- **Remote STT config** (`whisper_service`, `eventlab`) is no longer stored in SystemConfig.
  Configure remote STT engines via `POST /v1/model_registry` with `kind="stt"` entries (see below).
- **OmniVoice TTS config** is no longer stored in SystemConfig. Configure OmniVoice via
  `POST /v1/model_registry` with `kind="tts"` entries and store engine-specific settings in the
  `config` dict.
- **stt_local per-engine fields** have been removed — device/compute_type first, then the
  default model / model path and whisper decode tuning (`vosk_model_path`,
  `whisper_local_model`, `whisper_vad_filter`, `whisper_beam_size`,
  `whisper_condition_on_previous_text`, `whisper_initial_prompt`,
  `whisper_mlx_model_path`, `qwen3_asr_model`). Configure them per engine via the
  Model Registry `model_id=""` sentinel entries (`kind="stt"`, `engine="whisper_local"` /
  `"whisper_mlx"` / `"qwen3_asr"` / `"vosk"`), stored in the `config` dict — e.g.
  `{"default_model": "large-v3-turbo", "vad_filter": true, "beam_size": 1,
  "condition_on_previous_text": false, "initial_prompt": "", "device": "cpu",
  "compute_type": "int8"}` for `whisper_local`, `{"model_path": "..."}` for
  `vosk`/`whisper_mlx`.

`stt_local` now holds only engine-agnostic settings (model dir, sample rate, glossary,
profile preset, long-audio segmentation).

### `PUT /v1/system/config`
Update the system configuration. Send a partial or full body; absent fields retain their current
values. `SystemConfig` currently has no secret fields (`pyannote_auth_token`, the last one, moved to
the `PYANNOTE_AUTH_TOKEN` env var).

---

## Model Registry

The **Model Registry** stores engine configurations for STT, TTS, and LLM providers. Each entry
binds an engine to credentials, connection details, and engine-specific parameters.

### `GET /v1/model_registry`
List all model registry entries.

```json
{
  "success": true,
  "data": [
    {
      "id": "whisper_service_prod",
      "kind": "stt",
      "engine": "whisper_service",
      "model_id": "whisper-1",
      "label": "Whisper API (prod)",
      "stage": "stable",
      "enabled": true,
      "base_url": "https://api.openai.com/v1",
      "api_key": "sk-...abc",
      "config": { }
    },
    {
      "id": "qwen3_asr_local",
      "kind": "stt",
      "engine": "qwen3_asr",
      "model_id": "Qwen/Qwen3-ASR-0.6B",
      "label": "Qwen3 ASR (0.6B)",
      "stage": "stable",
      "enabled": true,
      "base_url": "",
      "api_key": "",
      "config": {
        "device": "mps",
        "compute_type": "float16"
      }
    },
    {
      "id": "omnivoice_standard",
      "kind": "tts",
      "engine": "omnivoice",
      "model_id": "k2-fsa/OmniVoice",
      "label": "OmniVoice",
      "stage": "stable",
      "enabled": true,
      "base_url": "",
      "api_key": "",
      "config": {
        "device": "mps",
        "dtype": "float16"
      }
    }
  ]
}
```

Fields:
- `id` — unique identifier for the entry
- `kind` — `"stt"`, `"tts"`, or `"llm"`
- `engine` — provider name, e.g. `whisper_service`, `eventlab`, `qwen3_asr`, `whisper_local`, `omnivoice`, `vieneu`, `openai`
- `model_id` — model identifier (HF repo, OpenAI model name, etc.)
- `label` — human-readable label for the UI
- `stage` — `"stable"` or `"experimental"`
- `enabled` — whether the entry is active
- `base_url` — for remote (STT/LLM) providers; OpenAI-compatible base URL
- `api_key` — masked on read (e.g. `sk-...abc`); updated only if non-blank
- `config` — engine-specific parameters dict (device, compute_type, dtype, timeout, etc.)

### `POST /v1/model_registry`
Create a new model registry entry.

Request body:
```json
{
  "kind": "stt",
  "engine": "whisper_service",
  "model_id": "whisper-1",
  "label": "Whisper API",
  "stage": "stable",
  "base_url": "https://api.openai.com/v1",
  "api_key": "sk-...",
  "config": {}
}
```

The endpoint validates the configuration by making a test call to the provider (e.g. transcribing
a silent WAV for STT, synthesizing sample text for TTS, or querying a chat endpoint for LLM).
Optional `sample_text` (default `"xin chào"`) customizes the validation text; used for TTS synthesis and LLM chat test calls.
If validation fails → `400` with the provider's error detail.

On success, returns the created entry with a masked `api_key`.

### `PATCH /v1/model_registry/{id}`
Update a model registry entry (partial update).

Request body:
```json
{
  "enabled": false,
  "stage": "experimental",
  "config": { "device": "cuda" }
}
```

Fields to update:
- `enabled` — toggle entry on/off
- `stage` — change to `"stable"` or `"experimental"`
- `base_url` — update endpoint URL (for remote providers)
- `api_key` — update credentials; blank or absent means "keep existing"
- `config` — replaces the entire config dict (not a merge) — submit the full desired config, not just the changed keys

If the entry is not found → `404`. On success, returns the updated entry with a masked `api_key`.

**Side effects:** Updating certain entries triggers runtime reinitialization:
- `kind="stt"` with `engine="whisper_service"` or `engine="eventlab"` → reinit remote STT providers
- `kind="stt"` with `engine="qwen3_asr"` and `config` changed → clear model cache
- `kind="tts"` with `engine="omnivoice"` → reset OmniVoice subprocess

---

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
  `{vosk_model_base_url}/{name}.zip` in the background, extracts into the configured
  STT model dir (admin System tab > stt_local group). Invalid names → `400`; a missing
  model surfaces as a job `error` (HTTP 404).
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
