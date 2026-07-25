# STT Row-Based Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the STT quick-test panel + its batch/stream endpoints select a specific Model Registry row (`engine` + `model_id`), like TTS — so qwencloud's two families (fun-asr, qwen3-asr-flash) are distinct, deterministic choices.

**Architecture:** Add an explicit `model` to the STT batch/stream request paths and thread it to the providers' existing `transcribe_bytes(model=...)` / an extended `open_stream(..., model=...)`. Rebuild the JS picker to list registry rows (mirror the TTS panel). No guard on multiple enabled rows — engine is a container, model is the choice.

**Tech Stack:** FastAPI + pytest; vanilla ES-module admin UI. Design spec: `docs/superpowers/specs/2026-07-25-stt-row-based-selection-design.md`.

## Global Constraints
- Engine = container; **model (row) is the unit of selection**. Multiple enabled rows per engine are supported; add NO "single enabled row" restriction anywhere.
- Selection value format matches TTS: `"engine|model_id"` in the UI select; backend receives `engine` + `model` (or `model_id`) separately.
- `open_stream` signature change is backward-compatible: add `model: str | None = None` as an optional trailing kwarg on the base and every override.
- Static-UI hazard (project memory): keep JS ASCII-only; edit with the Edit tool; **verify by Reading the file back** (`node --check` can false-pass smart-quote corruption).
- Tests in repo-root `tests/unit/`, run from repo root with the shared venv, one pytest at a time (concurrency guard). No push/deploy.
- Mirror the existing TTS analogs exactly: `static/js/tts-engines.js` `loadTtsEngines()` (row-based picker) and `POST /v1/tts/synthesize` (`model_id`) — STT should end up structurally identical.

## File Structure
- `apps/api_gateway/app/schemas/stt.py` — add `model` to `STTRequest`.
- `apps/api_gateway/app/api/routes/stt.py` — `model` Form on transcribe; `model` query param on the WS stream; thread both through.
- `apps/api_gateway/app/services/stt/base.py` — `open_stream` gains `model=None`; `BufferingStream` threads it into finalize.
- `apps/api_gateway/app/services/stt/providers/vosk_provider.py` — `open_stream` signature only (accept + ignore `model`).
- `apps/api_gateway/app/services/stt/providers/qwencloud_provider.py` — `open_stream(..., model=None)` resolves the exact row + family from `model`.
- `apps/api_gateway/app/services/stt/service.py` — `list_engines` qwencloud `detail`.
- `apps/api_gateway/app/static/js/stt-engines.js` — row-based picker.
- tests: `tests/unit/test_stt_routes.py`, `tests/unit/test_stt_stream.py`, `tests/unit/test_qwencloud_stt_provider.py`.

---

## Task 1: Batch path — `model` on transcribe + list_engines detail

**Files:** `schemas/stt.py`, `api/routes/stt.py` (transcribe), `services/stt/service.py`; Test: `tests/unit/test_stt_routes.py`.

**Interfaces:** Produces `STTRequest.model: str | None`; `POST /v1/stt/transcribe` accepts a `model` Form and calls `transcribe_bytes(audio, language, model=model)`.

- [ ] **Step 1: Failing tests** (append to `tests/unit/test_stt_routes.py`, following that file's TestClient/fixture style):
```python
def test_transcribe_passes_model_to_provider(client, monkeypatch):
    seen = {}
    async def fake_tb(self, audio_bytes, language=None, model=None):
        seen["model"] = model
        from app.schemas.stt import STTResult
        return STTResult(engine="qwencloud", text="ok", is_final=True)
    from app.services.stt.providers.qwencloud_provider import QwenCloudSttProvider
    monkeypatch.setattr(QwenCloudSttProvider, "transcribe_bytes", fake_tb)
    import io, wave
    buf = io.BytesIO(); w = wave.open(buf, "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b"\x00\x00"*320); w.close()
    resp = client.post("/v1/stt/transcribe",
        data={"engine": "qwencloud", "model": "fun-asr"},
        files={"audio": ("a.wav", buf.getvalue(), "audio/wav")})
    assert resp.status_code == 200, resp.text
    assert seen["model"] == "fun-asr"
```
(If the test file uses a different client fixture / auth, follow it. Pick whatever engine the file's other transcribe tests use if qwencloud needs registry setup — the point is that `model` reaches `transcribe_bytes`.)

- [ ] **Step 2: Run → fail** (`model` Form not accepted / not threaded).
`cd /Users/lugon/code/speech-text-transformer && .venv/bin/python -m pytest tests/unit/test_stt_routes.py -k model -v`

- [ ] **Step 3: Implement**
- `schemas/stt.py`: add `model: str | None = None` to `STTRequest`.
- `routes/stt.py` transcribe: add `model: str | None = Form(default=None)` to the signature; build `payload = STTRequest(engine=engine, language=language, model=model)`; change the transcribe call to `provider.transcribe_bytes(audio_bytes, payload.language, model=payload.model)`.
- `services/stt/service.py` `list_engines` qwencloud branch: replace `"detail": None` with a non-null status when configured, e.g. gather enabled qwencloud entries' `model_id`s and set `"detail": ", ".join(models) or None` (reuse the `list_all()` loop already in that branch).

- [ ] **Step 4: Run → pass** (`pytest tests/unit/test_stt_routes.py -v`).

- [ ] **Step 5: Commit** (`feat(stt): model on /v1/stt/transcribe + list_engines detail`).

---

## Task 2: Stream path — `open_stream(..., model=None)` threaded end to end

**Files:** `services/stt/base.py`, `providers/vosk_provider.py`, `providers/qwencloud_provider.py`, `api/routes/stt.py` (WS); Test: `tests/unit/test_qwencloud_stt_provider.py`, `tests/unit/test_stt_stream.py`.

**Interfaces:** `STTProvider.open_stream(self, sample_rate, language=None, model=None)`; qwencloud resolves the exact row + family from `model`.

- [ ] **Step 1: Failing test** (append to `tests/unit/test_qwencloud_stt_provider.py`, reuse `fake_connect`/`FakeWS`):
```python
@pytest.mark.asyncio
async def test_open_stream_selects_family_by_model(fake_connect, monkeypatch):
    # two enabled rows; passing model="fun-asr" must pick the fun-asr native WS,
    # not the first-enabled qwen3 row.
    def fake_find_sync(kind, engine, model_id):
        return {**_FUNASR_ENTRY, "model_id": model_id} if model_id == "fun-asr" else _QWEN_ENTRY
    monkeypatch.setattr(
        "app.services.stt.providers.qwencloud_provider.model_registry_store.find_sync", fake_find_sync)
    fake_connect["incoming"] = _funasr_msgs()
    stream = QwenCloudSttProvider().open_stream(16000, "vi", model="fun-asr")
    # FunAsrNativeStream connects to /api-ws/v1/inference with lowercase bearer
    await stream.accept(b"\x00\x00" * 160)
    assert "/api-ws/v1/inference" in fake_connect["url"]
```
(Adjust to the entry constants already defined in the test file. The key assertion: `model="fun-asr"` yields the fun-asr native stream deterministically.)

- [ ] **Step 2: Run → fail** (`open_stream` doesn't accept `model`).

- [ ] **Step 3: Implement**
- `base.py`: `def open_stream(self, sample_rate: int, language: str | None = None, model: str | None = None) -> STTStream:` — default returns `BufferingStream(self, sample_rate, language, model)`. Extend `BufferingStream.__init__(self, provider, sample_rate, language, model=None)`, store `self._model`, and in `finalize()` call `self._provider.transcribe_bytes(wav, self._language, model=self._model)`.
- `vosk_provider.py`: change `open_stream(self, sample_rate, language=None)` → `open_stream(self, sample_rate, language=None, model=None)` (ignore `model`; keep body).
- `qwencloud_provider.py` `open_stream`: accept `model=None`. Resolve the entry: `entry = self._entry_override or (model and model_registry_store.find_sync("stt", self.name, model)) or model_registry_store.find_enabled_sync("stt", self.name)`. Determine family from the explicit model first: `fam = _family(model or (entry or {}).get("model_id") or cfg.get("realtime_model"))`. Pick realtime model: `realtime_model = cfg.get("realtime_model") or ("fun-asr-realtime" if fam == "funasr" else "qwen3-asr-flash-realtime")`. Then branch: `fam == "funasr"` → `FunAsrNativeStream(...)`, else `QwenOaiRealtimeStream(...)`. (This removes reliance on `find_enabled_sync` when a model is supplied; keeps it only as the no-model fallback.)
- `routes/stt.py` WS `/v1/stt/stream`: read `model = websocket.query_params.get("model")`; call `provider.open_stream(sample_rate, language, model=model)`.

- [ ] **Step 4: Run → pass** (`pytest tests/unit/test_qwencloud_stt_provider.py tests/unit/test_stt_stream.py -v`). Confirm existing stream tests still pass with the new kwarg.

- [ ] **Step 5: Commit** (`feat(stt): thread model through open_stream for row-based streaming`).

---

## Task 3: Frontend — row-based STT quick-test picker

**Files:** `apps/api_gateway/app/static/js/stt-engines.js` (mirror `tts-engines.js`).

**Interfaces:** Consumes `GET /v1/model_registry/options?kind=stt` (rows) + the `model` params added in Tasks 1-2.

- [ ] **Step 1: Read the TTS analog** `apps/api_gateway/app/static/js/tts-engines.js` `loadTtsEngines()` and its submit handlers — this is the exact pattern to mirror (options from `/v1/model_registry/options?kind=tts` as `engine|model_id`; `/v1/tts/engines` used only for status). Also read the current `stt-engines.js` (`loadSttEngines` + the batch/stream submit handlers).

- [ ] **Step 2: Rewrite `loadSttEngines()`** to populate `#stt-engine` and `#stt-stream-engine` from `GET /v1/model_registry/options?kind=stt`, option value `"${o.engine}|${o.model_id}"`, label `"${o.engine} — ${o.model_id}"` (keep an availability/status line from `/v1/stt/engines` if the current UI shows one). Use the Edit tool; ASCII only.

- [ ] **Step 3: Update the batch + stream submit handlers** to split the selected `"engine|model_id"` and send `engine` + `model`: for batch, add `model` to the `FormData` posted to `/v1/stt/transcribe`; for stream, append `&model=<model_id>` to the `/v1/stt/stream` WS URL (alongside the existing `engine=`/`language=` params).

- [ ] **Step 4: Verify by Reading** the rewritten `stt-engines.js` regions — ASCII-only, balanced template literals, the `engine|model_id` split is correct, and the WS URL / FormData carry `model`. Then `node --check apps/api_gateway/app/static/js/stt-engines.js`.

- [ ] **Step 5: Commit** (`feat(admin-ui): row-based STT quick-test picker (engine|model_id)`).

---

## Task 4: Regression gate

- [ ] **Step 1: Run** `cd /Users/lugon/code/speech-text-transformer && .venv/bin/python -m pytest tests/unit/test_stt_routes.py tests/unit/test_stt_stream.py tests/unit/test_stt_ws.py tests/unit/test_qwencloud_stt_provider.py tests/integration/test_stt_ws.py -v` (drop any path that doesn't exist). All must pass; the `open_stream` kwarg change must not break vosk/whisper streaming tests. Record the count.
- [ ] **Step 2: Commit** only if a fix was needed.

## Notes
- Do NOT touch the profile STT selector (`profiles.js`) or TTS — already row-based.
- Do NOT add any "one enabled row per engine" guard (explicit user decision: engine = container).
- `find_sync` (store.py:135) and `find_enabled_sync` (store.py:157) both already exist.
