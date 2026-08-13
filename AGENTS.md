# AGENTS.md — coding guide for AI agents

Context for an agent writing or modifying code in **speech-text-transformer**: a local
FastAPI gateway unifying STT, TTS, and a voice Conversation loop (REST / WebSocket)
with a browser playground, serving browsers and IoT devices (ESP32 / Raspberry Pi).
Served live at `GET /agents-docs` (this file + all of `docs/` concatenated).

## Run, test, lint

```bash
make dev          # run with --reload (foreground)
make start|stop|restart|status|logs   # background service on :8000
make test         # pytest          (or: .venv/bin/python -m pytest -q)
make lint         # ruff            (make fmt to auto-fix)
```

- Python venv at `.venv`, **Python 3.12**. Run from the repo root. (3.12, not 3.13/3.14:
  the spacy/ML wheels the STT stack needs don't exist for the newer runtimes yet.
  `pyproject.toml` only declares `>=3.10`, so nothing stops you creating a venv that
  then can't install the extras.)
- Tests are hermetic (`tests/conftest.py`: mock engines on, no external Ollama/models).
  Add fast unit/integration tests; gate Apple-only/opus tests on availability + skip.
- **Always `make lint` and `make test` before committing.** Commit/push only when asked.

## Layout

```
apps/api_gateway/app/
  main.py                  # FastAPI app + middleware order + router wiring + lifespan
  core/                    # settings.py (pydantic-settings), audio.py, opus.py, errors.py,
                           # auth_guard.py (default-deny route classifier), actor.py
  api/routes/              # 24 routers — see the map below
  services/
    stt/{service.py, base.py, providers/}    # STTService registry + 11 engines
    tts/{service.py, base.py, providers/}    # TTSService registry + 8 engines
    conversation/          # session, endpointer (VAD), responder (LLM / echo), tools
    auth/                  # users, devices, pairing, tokens
    model_registry/        # unified STT/TTS/LLM entries + boot migrations + seed
    profiles/, mcp/, memory/, usage/, quota/, providers/, plugins/, history/, db/
    *_models.py            # download/select/delete managers (whisper, llm, tts, vosk)
  static/                  # index.html + js/ (35 ES modules, admin console)
apps/model_service/        # one-engine-per-container STT/TTS service (http_stt/http_tts)
docs/                      # api.md, architecture.md, runbook.md, device-integration.md
docs/superpowers/          # specs/ + plans/ — the design record for most features here
scripts/                   # setup + convert_phowhisper_mlx.sh + check_system.py
```

Route map — group → router file → what it owns:

| Group | Router | Owns |
|---|---|---|
| Core voice | `stt`, `tts`, `tts_profiles`, `conversation` | transcribe, synthesize, the voice loop |
| Devices | `lugo`, `devices` | the binary device protocol; pairing + device management |
| Identity | `auth`, `users` | sessions, bearer tokens, plugin tickets, accounts |
| Config | `profiles`, `memories`, `mcp`, `system` | per-profile config, chat memory, MCP servers, settings |
| Models | `model_registry`, `providers`, `recommend` | the unified engine registry, upstream accounts |
| Money | `usage`, `quotas` | spend attribution and limits |
| Console | `stats`, `sessions`, `ui`, `health`, `agents_docs` | Home counts, history, static UI |
| Extension | `plugins` | out-of-process feature plugins + their tickets |

**Adding a router is not just `include_router`.** `core/auth_guard.py` is
default-deny: an unclassified prefix is refused, and
`tests/unit/http/test_auth_guard_route_coverage.py` fails until you place the new
prefix in `_NO_AUTH_PREFIXES`, `_USER_PREFIXES`, `_USER_EXACT` or `_ADMIN_PREFIXES`.
Read that module's comments before choosing — the exact-vs-prefix and by-method
rules there exist to stop a user carve-out smuggling a caller into an admin handler
via path-param shadowing.

## Core conventions

- **Settings**: all config in `core/settings.py` (pydantic-settings, `.env`-backed).
  Add a typed field with a default; never read env directly elsewhere. `UPPER_SNAKE`
  env names map to the field. **Never commit `.env`** (real keys live there; gitignored).
- **STT provider**: subclass `STTProvider` (`services/stt/base.py`), implement
  `async transcribe_bytes(audio_bytes, language)`; optional `open_stream()` for native
  streaming, `warm()` for preloading, `detail()` for the UI label. Register in
  `STTService.providers` and add a branch in `list_engines()`. Heavy/optional engines
  must report `available=False` when their dep/model is missing (graceful fallback).
- **TTS provider**: subclass `TTSProvider`, implement `synthesize()`; `warm()`/`detail()`
  optional. Register in `TTSService`.
- **Model managers** (`*_models.py`) expose `snapshot()/download()/select()/delete()`
  over the HF hub cache or Ollama; surfaced in `/v1/models` and the System tab.
- **Errors**: raise `AppError` subclasses (→ JSON error via the global handler). In
  request handlers, convert provider exceptions to clean JSON (never leak a plain-text
  500 — clients parse JSON).
- **API responses**: REST returns `{"success": true, "data": …}`; WS emits
  `{"event": …}` / `StreamEvent`. Keep this shape.
- Match the surrounding code's style; keep comments at the existing density (explain
  *why*, not *what*).

## The conversation/gateway (most active area)

`WS /v1/conversation/stream` is a **text/audio → text/audio** gateway. Pipeline:
input (audio frames or `{"type":"text"}`) → optional STT → reply (echo / OpenAI-compat
LLM) → per-sentence TTS → output (text events + audio pushed as binary WebSocket
frames — WAV/MP3 by default or Opus, never a URL). VAD endpointer + barge-in. See
`docs/api.md` and `docs/device-integration.md` for the wire protocol.

## Platform gotchas (read before touching audio/STT)

- **MLX engines** (`whisper_mlx`) are **Apple-Silicon only** (mlx-whisper, Metal GPU).
  They auto-hide off Mac → callers fall back to `whisper` (CPU). CTranslate2/faster-whisper
  has **no GPU on macOS** (CPU only).
- **libopus** is a system lib. opuslib can't find Homebrew's on macOS unless
  `DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib` is set — the Makefile exports it. Opus
  code degrades gracefully (falls back to PCM16) when libopus is absent.
- **Vietnamese STT**: default is **qwen3_asr** — it beat faster-whisper on Vietnamese
  (FLEURS benchmark).
- TTS engines output different sample rates (VieNeu 48k, OmniVoice 24k) — resample when
  re-encoding (`core/audio.py: wav_file_to_pcm16`).
- **Opus playback pacing is per-connection, not just global.** `SessionRuntimeConfig.opus_pace`
  (`services/conversation/session.py`) overrides the global
  `system_config.conversation.conversation_opus_pace` when set; `None` (the default —
  what `api/routes/lugo.py` always uses) inherits the global value, so ESP32/RPi
  pacing is untouched. The web client (`api/routes/conversation.py`) sends
  `?opus_pace=0` to disable server-side throttling and rely on the browser's own
  `AudioContext` scheduling as the jitter buffer instead of the ~300ms
  `conversation_opus_prebuffer_frames` cushion sized for device ring buffers. If web
  playback stutters again, or an ESP32/RPi regression shows up after touching this
  code, see `docs/superpowers/specs/2026-07-28-web-audio-jitter-buffer-design.md`.

## Where to look

- Endpoints / schemas → `docs/api.md`
- Components / data flow → `docs/architecture.md`
- **Why something is the way it is** → `docs/decisions.md`. Read it before
  "fixing" an absence: several things that look missing are missing on purpose,
  and it records what would change the answer.
- Config / env vars / troubleshooting → `docs/runbook.md`
- Device (RPi/ESP32) protocol + reference client → `docs/device-integration.md`
