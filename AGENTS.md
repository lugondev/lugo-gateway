# AGENTS.md — coding guide for AI agents

Context for an agent writing or modifying code in **speech-text-transformer**: a local
FastAPI gateway unifying STT, TTS, and a voice Conversation loop (REST / WebSocket / SSE)
with a browser playground, serving browsers and IoT devices (ESP32 / Raspberry Pi).
Served live at `GET /agents-docs` (this file + all of `docs/` concatenated).

## Run, test, lint

```bash
make dev          # run with --reload (foreground)
make start|stop|restart|status|logs   # background service on :8000
make test         # pytest          (or: .venv/bin/python -m pytest -q)
make lint         # ruff            (make fmt to auto-fix)
```

- Python venv at `.venv`, Python 3.14. Run from the repo root.
- Tests are hermetic (`tests/conftest.py`: mock engines on, no external Ollama/models).
  Add fast unit/integration tests; gate Apple-only/opus tests on availability + skip.
- **Always `make lint` and `make test` before committing.** Commit/push only when asked.

## Layout

```
apps/api_gateway/app/
  main.py                  # FastAPI app + router wiring
  core/                    # settings.py (pydantic-settings), audio.py, opus.py, errors.py
  api/routes/              # health, stt, tts, events, conversation, system, ui, agents_docs
  services/
    stt/{service.py, base.py, providers/}    # STTService registry + providers
    tts/{service.py, base.py, providers/}    # TTSService registry + providers
    conversation/          # endpointer (VAD), responder (LLM / echo)
    *_models.py            # download/select/delete managers (whisper, llm, tts, vosk)
  static/                  # index.html + app.js (playground UI)
docs/                      # api.md, architecture.md, runbook.md, device-integration.md
scripts/                   # setup + convert_phowhisper_mlx.sh + rpi_voice_client.py
```

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
- **API responses**: REST returns `{"success": true, "data": …}`; WS/SSE emit
  `{"event": …}` / `StreamEvent`. Keep this shape.
- Match the surrounding code's style; keep comments at the existing density (explain
  *why*, not *what*).

## The conversation/gateway (most active area)

`WS /v1/conversation/stream` is a **text/audio → text/audio** gateway. Pipeline:
input (audio frames or `{"type":"text"}`) → optional STT → reply (echo / OpenAI-compat
LLM) → per-sentence TTS → output (text events + audio as `audio_url` or pushed Opus
frames). VAD endpointer + barge-in. See `docs/api.md` and `docs/device-integration.md`
for the wire protocol.

## Platform gotchas (read before touching audio/STT)

- **MLX engines** (`whisper_mlx`) are **Apple-Silicon only** (mlx-whisper, Metal GPU).
  They auto-hide off Mac → callers fall back to `whisper` (CPU). CTranslate2/faster-whisper
  has **no GPU on macOS** (CPU only).
- **libopus** is a system lib. opuslib can't find Homebrew's on macOS unless
  `DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib` is set — the Makefile exports it. Opus
  code degrades gracefully (falls back to PCM16) when libopus is absent.
- **Vietnamese STT**: default is **PhoWhisper** (VinAI fine-tune), not vanilla Whisper.
- TTS engines output different sample rates (VieNeu 48k, OmniVoice 24k) — resample when
  re-encoding (`core/audio.py: wav_file_to_pcm16`).

## Where to look

- Endpoints / schemas → `docs/api.md`
- Components / data flow → `docs/architecture.md`
- Config / env vars / troubleshooting → `docs/runbook.md`
- Device (RPi/ESP32) protocol + reference client → `docs/device-integration.md`
