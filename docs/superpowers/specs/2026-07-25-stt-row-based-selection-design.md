# STT Row-Based Selection (align with TTS) — Design

**Date:** 2026-07-25
**Status:** Approved design → implementation
**Scope:** Make the STT quick-test panel and its endpoints select a specific Model Registry **row** (`engine` + `model_id`) instead of just an `engine`, mirroring TTS. Fixes the qwencloud case where two enabled rows (fun-asr, qwen3-asr-flash) collapse into one ambiguous "qwencloud (remote)" choice.

## 1. Problem (verified in code)
- The STT quick-test panel (`static/js/stt-engines.js` → `#stt-engine`/`#stt-stream-engine`) builds options from `GET /v1/stt/engines` = `stt_service.list_engines()`, which iterates **engines** (one row per engine key), not registry rows. So multiple qwencloud rows show as a single "qwencloud (remote)".
- `POST /v1/stt/transcribe` (`STTRequest`) and `WS /v1/stt/stream` have **no `model`/`model_id` field**; they call `transcribe_bytes(..., model=None)` / `open_stream(sample_rate, language)`. For qwencloud that falls into `find_enabled(kind, engine)` = "first-enabled-wins" → **non-deterministic** family when both rows are enabled.
- `list_engines` qwencloud branch sets `detail=None` (spec said `detail=model id`) — cosmetic deviation.
- **Contrast — already correct:** the profile STT editor (`profiles.js` `renderProfileSttModelSelect`) is ALREADY row-based (`/v1/model_registry/options?kind=stt` → `engine|model_id` → `SttConfig.engine`/`.model` → exact row resolve). TTS is row-based end to end (`tts-engines.js`, `/v1/tts/synthesize` with `model_id`). Only the STT quick-test surface lags.

## 2. Locked decisions
- **Model is the unit of selection; engine is a container** (like a provider). Multiple enabled rows per engine are a supported, normal state — **no "single enabled row" guard** is added anywhere.
- Full sync for **both batch and stream** STT test paths, mirroring the TTS test panel exactly.
- `find_enabled(kind, engine)` must no longer be the selection mechanism on the main STT request paths; selection is always an explicit `(engine, model_id)` row (it may remain a last-resort fallback when no model is supplied, for backward compatibility).

## 3. Changes

### Frontend — `static/js/stt-engines.js`
- Rewrite `loadSttEngines()` to populate `#stt-engine` and `#stt-stream-engine` from `GET /v1/model_registry/options?kind=stt` with option value `"engine|model_id"` and a label like `"engine — model_id"` (mirror `tts-engines.js` `loadTtsEngines()`). Keep `/v1/stt/engines` only for availability/status text (or drop if unused by this panel).
- On submit (batch + stream), split the selected `"engine|model_id"` and send both to the endpoints.

### Backend — batch: `schemas/stt.py` + `routes/stt.py`
- Add `model: str | None = None` to `STTRequest` and a `model` `Form(...)` param on `POST /v1/stt/transcribe`; pass it to `provider.transcribe_bytes(audio_bytes, language, model=...)`.

### Backend — stream: `routes/stt.py` + `services/stt/base.py` + providers
- `WS /v1/stt/stream`: read a `model` query param; pass to `open_stream`.
- Extend the streaming contract: `STTProvider.open_stream(self, sample_rate, language=None, model=None)` (backward-compatible optional kwarg). `BufferingStream` threads `model` into the finalize transcribe. Providers that ignore model (vosk) keep working unchanged.
- `QwenCloudSttProvider.open_stream(sample_rate, language=None, model=None)`: when `model` is given, resolve the SPECIFIC registry row (`find_enabled_sync` replaced by an exact `(engine, model_id)` lookup via a sync `find_sync`), pick family from `model` (or the row's `config.realtime_model`), so the stream deterministically uses the chosen family. Falls back to the current first-enabled behavior only when no model is supplied.

### Backend — `services/stt/service.py`
- `list_engines` qwencloud branch: set `detail` to something meaningful instead of `None` (e.g. the enabled qwencloud entries' model ids, or a count) — since options no longer come from here, this is status text only, but should not misleadingly show nothing.

## 4. Out of scope
- Profile STT selection (already row-based — no change).
- TTS (already row-based).
- Any restriction on how many rows an engine may have enabled (explicitly rejected — engine = container).

## 5. Testing
- Backend: `POST /v1/stt/transcribe` with `model=` routes to the exact row (mock provider `transcribe_bytes`, assert `model` received). `WS /v1/stt/stream` with `model=` reaches `open_stream(..., model=...)`. qwencloud `open_stream(model="fun-asr")` selects the fun-asr family deterministically even with a qwen3 row also enabled.
- Backend: `open_stream` signature back-compat — existing providers/tests still pass with the new optional kwarg.
- Backend: `list_engines` qwencloud `detail` is non-None when configured.
- Frontend: no JS harness — verify `stt-engines.js` by Read (ASCII-only) + `node --check`; backend options endpoint already returns per-row data (contract the JS reads).

## 6. Files touched
- `apps/api_gateway/app/static/js/stt-engines.js`
- `apps/api_gateway/app/schemas/stt.py`
- `apps/api_gateway/app/api/routes/stt.py`
- `apps/api_gateway/app/services/stt/base.py` (open_stream signature + BufferingStream)
- `apps/api_gateway/app/services/stt/providers/qwencloud_provider.py` (open_stream model param) and `vosk_provider.py` (signature only)
- `apps/api_gateway/app/services/stt/service.py` (list_engines detail)
- tests: `tests/unit/test_stt_routes.py`, `tests/unit/test_stt_stream.py` (or `test_stt_ws.py`), `tests/unit/test_qwencloud_stt_provider.py`
