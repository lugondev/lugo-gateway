# Close the Metering Gaps — and Make a Future Gap Fail CI

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the last two paths that spend money without being metered or quota-gated, then make it structurally impossible for a future paid call site to be added without someone deliberately classifying it — and impossible for a metering test to claim coverage it does not have.

**Architecture:** Three parts. (1) The two leaks — `WS /v1/stt/stream` and `POST /v1/tts/stream` — get identity, a gate, and metering, following the per-turn shape the conversation core already uses. (2) A **completeness harness**: one test enumerates every provider-invoking call in the source and requires each to appear in an explicit table with a status, a reason, and the name of a test that covers it; a second test drives every REST/WS entry point end to end and asserts a real `usage_events` row lands in the database. A new call site, a second call in an already-classified file, or a classification naming a test that does not exist all fail CI. (3) The remaining correctness and legibility items, so enforcement is both correct and visible to the person it affects.

**Tech Stack:** Python 3.12 (FastAPI, SQLAlchemy async, pytest), vanilla ES-module JS for the admin static UI, React + TypeScript for `lugo-web-client`.

## Global Constraints

- **Python:** always `.venv/bin/python` (the venv is 3.12).
- **Test scope:** `tests/unit` of this repo. The React client has its own suite (`npm test` inside `lugo-web-client`) — run it only for the task that touches that submodule.
- **Never push.** `main` auto-deploys to production. Commit locally only.
- **Branch:** do all work on `feat/close-metering-gaps` (create it off `main` before Task 1). Other sessions share this working tree — do not switch branches mid-task.
- **Pre-existing dirt:** the working tree has unrelated modified files (two under `docs/superpowers/` dated 2026-07-25, and five submodule gitlinks). Never stage, commit, or revert them. Stage by path; never `git add -A`.
- **Never write to `data/app.db`.** Use pytest (its autouse fixture gives each test a temp DB), or set `DATABASE_URL` to a temp file and verify it took effect before writing. An agent on an earlier branch wrote to the dev DB by accident.
- **ASCII quotes only** in `.js` / `.html` / `.tsx`. `node --check` does not catch smart quotes inside string literals; grep for `[‘’“”]` and read the changed lines back.
- **Metering must never raise into a caller.** Every `record_usage` call is wrapped in `try/except Exception` + `logger.warning`.
- **Every gate stays fail-open.** `quota_gate` may only ever raise `QuotaExceededError`; resolution and lookup failures degrade to `provider_id = ""`, which still enforces user- and global-scope quotas.
- **A test that cannot fail is a defect.** For every behavioral test in this plan, confirm it RED before the production change and record the actual failure output in your report. If a test passes before the change, say so and either strengthen it or state plainly what it does and does not prove — do not leave it looking like proof.

---

## Reference: the complete inventory of paid call sites

`grep -rn "transcribe_bytes\|\.synthesize(\|reply_stream(\|\.reply(\|embed_texts\|open_stream" apps/api_gateway/app --include="*.py"`, minus definitions and the provider implementations themselves, yields 31 call sites. All of them, classified:

| Call site | Kind | Metered | Gated |
|---|---|---|---|
| `api/routes/conversation.py` `reply_stream` + `reply` (`/chat`) | llm | yes | yes |
| `api/routes/stt.py` `transcribe_bytes` (`/v1/stt/transcribe`) | stt | yes | yes |
| `api/routes/tts.py` `synthesize` (`/v1/tts/synthesize`) | tts | yes | yes |
| `api/routes/livehost.py` `transcribe_bytes`, `synthesize`, 2× `reply_stream` | stt/tts/llm | yes | yes |
| `services/conversation/session.py` `transcribe_bytes`, 2× `synthesize`, 2× `reply_stream` | stt/tts/llm | yes | yes |
| `services/memory/extractor.py` `embed_texts_with_usage` | embed | yes | yes |
| `services/memory/retriever.py` `embed_texts_with_usage` | embed | yes | by the turn |
| `services/stt/segmented.py` 2× `transcribe_bytes` | stt | by its caller | by its caller |
| **`api/routes/stt.py` `open_stream` (`WS /v1/stt/stream`)** | stt | **NO** | **NO** |
| **`services/stt/base.py` + `streaming_chunked.py` `transcribe_bytes`** | stt | **NO** | **NO** |
| **`api/routes/tts.py` `synthesize` inside `create_stream_job`** | tts | **NO** | **NO** |
| `api/routes/model_registry.py` 3× provider calls + `embed_texts` (add-time test) | all | **NO** | no, by design |

Four groups need action, and one needs a documented decision:

- **`WS /v1/stt/stream`** (Task 1). The route resolves an identity already (`resolve_ws_identity`) but never gates or meters. The two adapter call sites in `services/stt/base.py` and `services/stt/streaming_chunked.py` are reached only from this path — metering the route covers them, and metering them individually would double-count.
- **`POST /v1/tts/stream`** (Task 2). Worse than unmetered: `create_stream_job(payload: TTSRequest)` takes no `Request`, so it has no identity at all.
- **Model Registry add-time test calls** (Task 5). These are real provider calls that really cost money — one short "xin chào" each. Decision, to be implemented and documented: **meter them, do not gate them.** An admin must be able to validate a provider's credentials while over quota; gating that would trap them in a state they cannot fix. Metering makes the spend visible.
- Everything already marked yes stays as it is. Do not add a second metering call anywhere — `segmented.py` in particular is covered by the single row its caller records for the whole clip, and adding one there would double-count.

### Interfaces you will work against (verified — do not re-derive)

- `record_usage(*, user_id, profile_id, kind, engine, model_id, unit, native_amount, prompt_tokens=None, completion_tokens=None, request_id=None, status="ok")` — `app.services.usage.recorder`. Swallows its own errors; resolves blank engine/model internally via `resolve_usage_model`; computes `cost_usd` from the model's price.
- `quota_gate(*, user_id, provider_id, kind="", engine="", model_id="", profile_id="")` — `app.services.quota.gate`. Raises only `QuotaExceededError`. A block with a non-empty `kind` writes a `status="blocked"` audit row.
- `resolve_usage_model(kind, engine, model_id) -> tuple[str, str]` and `resolve_llm_pair(responder, pinned_engine, pinned_model) -> tuple[str, str]` — `app.services.usage.attribution`.
- `current_spend(*, scope, scope_id, period) -> float` — `app.services.quota.gate`.
- `resolve_ws_identity(websocket)` — `app.core.auth_guard`; returns an object with `.user_id` (may be `None`), or `None` when unauthorized.
- `current_user_id(request)` — `app.core.actor`.
- Units by kind, matching every existing call site: `stt` → `"seconds"`, `tts` → `"chars"`, `llm`/`embed` → `"tokens"`.
- Test style: newer files are marker-free async (asyncio auto mode) with `await init_db()`; `tests/unit/test_routes_usage_metering.py` is the closest reference for driving routes with `TestClient` and asserting real rows. `quota_store` caches in memory — call `quota_store.invalidate()` in any test that creates quotas.
- The static UI has no JS test harness: verification there is `node --check`, the smart-quote grep, and reading the file back.

---

## Phase A — close the two leaks

### Task 1: Meter and gate `WS /v1/stt/stream`

**Files:**
- Modify: `apps/api_gateway/app/api/routes/stt.py` (the `stt_stream` websocket handler)
- Test: `tests/unit/test_stt_stream_metering.py`

**Interfaces:**
- Consumes: `quota_gate`, `QuotaExceededError`, `resolve_usage_model`, `record_usage`, `model_registry_store`.
- Produces: no new API. The socket now refuses to start when a quota is over limit, and records `kind="stt"` rows for the audio it processed.

**Design decisions, and why:**
- **Gate at connect, and again at each `flush`/`end`.** A flush is this endpoint's unit of work, the analogue of a turn; gating only at connect would let one long-lived socket run unbounded after the limit is hit. On a mid-session block, emit an `error` event and close — do not silently keep accepting audio the server will not transcribe.
- **Meter per finalize, not per frame.** A frame is 20-100 ms; a row per frame would write thousands of rows for one minute of speech. Accumulate `audio_seconds` from the raw PCM (`len(frame) / 2 / sample_rate` — PCM16 mono, 2 bytes per sample) and record one row per `flush`/`end`, plus a final row at disconnect for any audio that was never flushed. Reset the accumulator after each row so nothing is counted twice.
- **Count the audio received, not the audio kept.** Accumulate from the frame as it arrives, BEFORE `preprocess_pcm16` — VAD can drop most of a frame, but the client still sent it and a per-minute provider still bills for what it processed. Metering the post-VAD length would systematically under-report.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_stt_stream_metering.py`:

```python
"""WS /v1/stt/stream was the last STT path that spent money with no usage row
and no quota check. A long-lived socket is the worst place for that gap: it can
transcribe indefinitely."""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.schemas.stt import STTResult
from app.services.db.engine import db_session, init_db
from app.services.db.models import UsageEvent
from app.services.model_registry.store import model_registry_store
from app.services.quota.store import quota_store
from app.services.stt.base import STTProvider, STTStream
from app.services.stt.service import stt_service
from app.services.usage.recorder import record_usage


class _StubStream(STTStream):
    async def accept(self, frame: bytes):
        return []

    async def finalize(self) -> STTResult:
        return STTResult(text="xin chao", is_final=True)


class _StubSTT(STTProvider):
    name = "stub-stream-stt"

    def available(self) -> bool:
        return True

    async def transcribe_bytes(self, audio: bytes, language=None, model=None) -> STTResult:
        return STTResult(text="xin chao", is_final=True)

    def open_stream(self, sample_rate, language=None, model=None):
        return _StubStream()


@pytest.fixture
def _stub_engine():
    stt_service.providers["stub-stream-stt"] = _StubSTT()
    yield
    stt_service.providers.pop("stub-stream-stt", None)


async def _rows(kind="stt"):
    async with db_session() as s:
        rows = (await s.execute(select(UsageEvent))).scalars().all()
    return [r for r in rows if r.kind == kind]


# 16 kHz mono PCM16: 3200 bytes = 1600 samples = 0.1 s
_FRAME = b"\x00\x00" * 1600


async def test_stream_records_the_audio_seconds_it_received(_stub_engine):
    await init_db()
    quota_store.invalidate()
    client = TestClient(app)
    with client.websocket_connect(
        "/v1/stt/stream?engine=stub-stream-stt&sample_rate=16000&denoise=false&vad=false"
    ) as ws:
        assert ws.receive_json()["event_type"] == "session_started"
        for _ in range(5):  # 5 x 0.1 s = 0.5 s
            ws.send_bytes(_FRAME)
        ws.send_text(json.dumps({"type": "end"}))
        # Drain until the final result arrives so the server has finalized.
        for _ in range(5):
            event = ws.receive_json()
            if event["event_type"] == "final":
                break

    rows = await _rows()
    assert len(rows) == 1, f"expected one row per finalize, got {len(rows)}"
    assert rows[0].engine == "stub-stream-stt"
    assert rows[0].unit == "seconds"
    assert abs(rows[0].native_amount - 0.5) < 1e-6


async def test_audio_is_counted_before_vad_can_drop_it(_stub_engine):
    """A per-minute provider bills for what it processed. Metering the
    post-VAD length would systematically under-report."""
    await init_db()
    quota_store.invalidate()
    client = TestClient(app)
    with client.websocket_connect(
        "/v1/stt/stream?engine=stub-stream-stt&sample_rate=16000&denoise=false&vad=true"
    ) as ws:
        assert ws.receive_json()["event_type"] == "session_started"
        for _ in range(3):
            ws.send_bytes(_FRAME)  # pure silence: VAD will drop nearly all of it
        ws.send_text(json.dumps({"type": "end"}))
        for _ in range(5):
            if ws.receive_json()["event_type"] == "final":
                break

    rows = await _rows()
    assert len(rows) == 1
    assert abs(rows[0].native_amount - 0.3) < 1e-6, "must count received audio, not post-VAD audio"


async def test_two_flushes_produce_two_rows_without_double_counting(_stub_engine):
    await init_db()
    quota_store.invalidate()
    client = TestClient(app)
    with client.websocket_connect(
        "/v1/stt/stream?engine=stub-stream-stt&sample_rate=16000&denoise=false&vad=false"
    ) as ws:
        assert ws.receive_json()["event_type"] == "session_started"
        for _ in range(2):
            ws.send_bytes(_FRAME)
        ws.send_text(json.dumps({"type": "flush"}))
        for _ in range(5):
            if ws.receive_json()["event_type"] == "final":
                break
        for _ in range(3):
            ws.send_bytes(_FRAME)
        ws.send_text(json.dumps({"type": "end"}))
        for _ in range(5):
            if ws.receive_json()["event_type"] == "final":
                break

    rows = sorted(await _rows(), key=lambda r: r.native_amount)
    assert len(rows) == 2, f"expected one row per flush, got {len(rows)}"
    assert abs(rows[0].native_amount - 0.2) < 1e-6
    assert abs(rows[1].native_amount - 0.3) < 1e-6
    total = sum(r.native_amount for r in rows)
    assert abs(total - 0.5) < 1e-6, "the same audio must not be counted twice"


async def test_an_over_quota_socket_is_refused_before_any_audio(_stub_engine):
    await init_db()
    quota_store.invalidate()
    await model_registry_store.create(
        "stt", "stub-stream-stt", "stub-model", "Stub",
        config={"provider_id": "prov-s", "price": {"unit": "minute", "rate": 60.0}},
    )
    await record_usage(user_id="", profile_id="", kind="stt", engine="stub-stream-stt",
                       model_id="stub-model", unit="seconds", native_amount=120)  # $120
    await quota_store.create(scope="global", scope_id="", limit_usd=1.0, period="monthly")

    client = TestClient(app)
    with client.websocket_connect(
        "/v1/stt/stream?engine=stub-stream-stt&sample_rate=16000&denoise=false&vad=false"
    ) as ws:
        event = ws.receive_json()
        assert event["event_type"] == "error", f"expected a refusal, got {event}"
        assert "quota exceeded" in event["payload"]["message"]

    # The refusal itself must be audited, and must not have transcribed anything.
    blocked = [r for r in await _rows() if r.status == "blocked"]
    assert len(blocked) == 1
    served = [r for r in await _rows() if r.status == "ok" and r.native_amount < 120]
    assert served == [], "a refused socket must not record served audio"


async def test_a_disconnect_without_a_flush_still_records(_stub_engine):
    """Audio sent and then abandoned was still processed by the provider."""
    await init_db()
    quota_store.invalidate()
    client = TestClient(app)
    with client.websocket_connect(
        "/v1/stt/stream?engine=stub-stream-stt&sample_rate=16000&denoise=false&vad=false"
    ) as ws:
        assert ws.receive_json()["event_type"] == "session_started"
        for _ in range(4):
            ws.send_bytes(_FRAME)
        # No flush/end: just drop the connection.

    rows = await _rows()
    assert len(rows) == 1
    assert abs(rows[0].native_amount - 0.4) < 1e-6
```

- [ ] **Step 2: Run the test to see it fail**

Run: `.venv/bin/python -m pytest tests/unit/test_stt_stream_metering.py -q`
Expected: every test fails — no rows are recorded and no refusal happens. Record the actual output.

If a test fails for a reason other than "no usage row" — for instance the stub provider not fitting `STTProvider`'s interface, or `websocket_connect` needing a different drain sequence — fix the TEST harness until it fails for the right reason, and report what you changed. Do not touch production code to make a broken harness pass.

- [ ] **Step 3: Add the gate at connect**

In `stt_stream`, after `identity` is resolved and the engine/model/sample_rate query params are read, but BEFORE `provider.open_stream(...)`:

```python
    from app.services.model_registry.store import model_registry_store
    from app.services.quota.gate import QuotaExceededError, quota_gate
    from app.services.usage.attribution import resolve_usage_model

    async def _quota_message(user_id: str) -> str:
        """"" when the socket may proceed, else the refusal to send. Resolving the
        pair first is what lets a provider-scoped quota match (see the STT route)."""
        try:
            usage_engine, usage_model = await resolve_usage_model("stt", engine, model or "")
            provider_id = ""
            try:
                entry = await model_registry_store.find("stt", usage_engine, usage_model)
                provider_id = (entry or {}).get("config", {}).get("provider_id", "") if entry else ""
            except Exception:  # noqa: BLE001 - a registry hiccup must never block a socket
                provider_id = ""
            await quota_gate(
                user_id=user_id, provider_id=provider_id,
                kind="stt", engine=usage_engine, model_id=usage_model,
            )
        except QuotaExceededError as exc:
            return str(exc)
        except Exception as exc:  # noqa: BLE001 - fail-open
            logger.warning("stt stream quota check failed open: %s", exc)
        return ""

    caller_id = identity.user_id or ""
    refusal = await _quota_message(caller_id)
    if refusal:
        await websocket.send_json(
            {"event_type": "error", "session_id": session_id, "payload": {"message": refusal}}
        )
        await websocket.close()
        return
```

Place this after `await websocket.accept(...)` and after `session_id` exists, so the refusal can be sent on the socket. Read the surrounding code and match how the existing engine-failure path emits its error and closes.

- [ ] **Step 4: Accumulate and record the audio**

Add an accumulator beside `sequence`, and a recorder:

```python
    pending_seconds = 0.0

    async def _record_stream_usage() -> None:
        """One row per flush/end for the audio received since the last row.

        Per frame would be thousands of rows a minute; per session would lose the
        work of a socket that never disconnects cleanly. Reset after recording so
        the same audio can never be counted twice.
        """
        nonlocal pending_seconds
        seconds, pending_seconds = pending_seconds, 0.0
        if seconds <= 0:
            return
        try:
            await record_usage(
                user_id=caller_id, profile_id="",
                kind="stt", engine=engine, model_id=model or "",
                unit="seconds", native_amount=seconds,
            )
        except Exception as exc:  # noqa: BLE001 - metering must never break the stream
            logger.warning("stt stream usage metering failed: %s", exc)
```

In the `message.get("bytes") is not None` branch, count the frame BEFORE preprocessing:

```python
                frame = message["bytes"]
                # Count what the client sent, before VAD/denoise can shrink it: a
                # per-minute provider bills for what it processed.
                pending_seconds += len(frame) / 2 / sample_rate
```

In the `flush`/`end` control branch, after the final result is emitted, re-gate and record:

```python
                    await _record_stream_usage()
                    refusal = await _quota_message(caller_id)
                    if refusal:
                        sequence += 1
                        await _emit(
                            websocket, channel,
                            StreamEvent(event_type="error", session_id=session_id,
                                        sequence=sequence, payload={"message": refusal}),
                        )
                        break
```

And in the handler's `finally` (or immediately after the receive loop ends — match the file's existing cleanup structure), record whatever was never flushed:

```python
        await _record_stream_usage()
```

Read the existing loop and cleanup carefully before inserting: the `end` control may already break out of the loop, and the final `_record_stream_usage()` must run exactly once on every exit path, including an exception.

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/unit/test_stt_stream_metering.py tests/unit/test_stt_stream.py -q` (find the existing stream test file with `ls tests/unit | grep stt`; every one of them must still pass).
Expected: PASS. Report the counts.

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/api/routes/stt.py tests/unit/test_stt_stream_metering.py
git commit -m "fix(usage): meter and gate the STT streaming socket"
```

---

### Task 2: Give `POST /v1/tts/stream` an identity, a gate, and metering

**Files:**
- Modify: `apps/api_gateway/app/api/routes/tts.py` (`create_stream_job`)
- Test: `tests/unit/test_tts_stream_metering.py`

**Interfaces:**
- Consumes: `current_user_id`, `quota_gate`, `QuotaExceededError`, `resolve_usage_model`, `record_usage`, `model_registry_store`.
- Produces: `create_stream_job(payload: TTSRequest, request: Request)` — the added parameter is what gives the job an identity to attribute. FastAPI injects `Request` positionally-agnostically, so no caller changes.

**Design decisions:**
- **Gate synchronously, before the job is spawned.** The endpoint returns a `job_id` and streams over SSE; refusing after the client has a job id would mean reporting the refusal through the event channel, which every client would have to learn to read. A 429 from the POST is the same contract `/v1/tts/synthesize` already has.
- **Meter per chunk, not per job.** The job synthesizes one segment at a time and can be cancelled halfway; `session.py` meters per sentence for exactly this reason. Per chunk means a cancelled job is billed for what it actually synthesized.
- **Capture the identity before the background task starts.** `current_user_id(request)` must be read in the request scope — the `Request` object must not be touched from inside the spawned task, which outlives the request.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_tts_stream_metering.py`:

```python
"""POST /v1/tts/stream spawned a background job that synthesized speech with no
usage row, no quota check, and no identity at all -- the endpoint did not even
take a Request."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.schemas.tts import TTSResult
from app.services.db.engine import db_session, init_db
from app.services.db.models import UsageEvent
from app.services.model_registry.store import model_registry_store
from app.services.quota.store import quota_store
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service
from app.services.usage.recorder import record_usage


class _StubTTS(TTSProvider):
    name = "stub-stream-tts"

    def available(self) -> bool:
        return True

    async def synthesize(self, request) -> TTSResult:
        return TTSResult(audio_url="/artifacts/x.wav", duration_seconds=0.1,
                         sample_rate=16000, engine=self.name)


@pytest.fixture
def _stub_engine():
    tts_service.providers["stub-stream-tts"] = _StubTTS()
    yield
    tts_service.providers.pop("stub-stream-tts", None)


async def _rows(kind="tts"):
    async with db_session() as s:
        rows = (await s.execute(select(UsageEvent))).scalars().all()
    return [r for r in rows if r.kind == kind]


async def test_the_stream_job_meters_every_chunk_it_synthesizes(_stub_engine):
    await init_db()
    quota_store.invalidate()
    client = TestClient(app)
    # Two sentences -> two segments -> two synthesize calls.
    resp = client.post("/v1/tts/stream", json={
        "text": "Xin chao ban. Hom nay the nao?", "engine": "stub-stream-tts",
    })
    assert resp.status_code == 200, resp.text

    # The job runs in the background; give it a moment to finish.
    for _ in range(50):
        rows = await _rows()
        if len(rows) >= 2:
            break
        await asyncio.sleep(0.02)

    rows = await _rows()
    assert len(rows) >= 2, f"expected one row per synthesized chunk, got {len(rows)}"
    assert all(r.engine == "stub-stream-tts" for r in rows)
    assert all(r.unit == "chars" for r in rows)
    total_chars = sum(r.native_amount for r in rows)
    assert total_chars > 0
    # Every chunk's characters are counted, and only the text sent to the provider.
    assert total_chars <= len("Xin chao ban. Hom nay the nao?")


async def test_an_over_quota_stream_job_is_refused_with_429_and_never_starts(_stub_engine):
    await init_db()
    quota_store.invalidate()
    await model_registry_store.create(
        "tts", "stub-stream-tts", "stub-model", "Stub",
        config={"provider_id": "prov-t", "price": {"unit": "1k_chars", "rate": 100.0}},
    )
    await record_usage(user_id="", profile_id="", kind="tts", engine="stub-stream-tts",
                       model_id="stub-model", unit="chars", native_amount=1000)  # $100
    await quota_store.create(scope="global", scope_id="", limit_usd=1.0, period="monthly")

    client = TestClient(app)
    resp = client.post("/v1/tts/stream", json={"text": "Xin chao", "engine": "stub-stream-tts"})
    assert resp.status_code == 429, resp.text
    assert "quota exceeded" in resp.json()["detail"]

    await asyncio.sleep(0.1)  # a spawned job would have had time to record by now
    served = [r for r in await _rows() if r.status == "ok" and r.native_amount < 1000]
    assert served == [], "a refused job must not synthesize anything"
    blocked = [r for r in await _rows() if r.status == "blocked"]
    assert len(blocked) == 1, "the refusal must be audited"
```

- [ ] **Step 2: Run the test to see it fail**

Run: `.venv/bin/python -m pytest tests/unit/test_tts_stream_metering.py -q`
Expected: both fail — no rows at all, and the over-quota POST returns 200 with a job id. Record the output.

- [ ] **Step 3: Add identity and the gate**

In `apps/api_gateway/app/api/routes/tts.py`, change the signature and add the pre-flight. `Request` and `current_user_id` are already imported in this module (the `/synthesize` handler uses both) — verify before adding imports.

```python
@router.post("/stream")
async def create_stream_job(payload: TTSRequest, request: Request) -> dict:
    # Quota pre-flight, synchronous: this endpoint returns a job_id and streams
    # over SSE, so a refusal has to happen here -- reporting it through the event
    # channel would make every client learn a second failure path. Same 429
    # contract as /v1/tts/synthesize.
    from app.services.model_registry.store import model_registry_store
    from app.services.quota.gate import quota_gate, QuotaExceededError
    from app.services.usage.attribution import resolve_usage_model

    usage_engine, usage_model_id = "", ""
    provider_id = ""
    try:
        usage_engine, usage_model_id = await resolve_usage_model(
            "tts", payload.engine, payload.model_id or ""
        )
        entry = await model_registry_store.find("tts", usage_engine, usage_model_id)
        provider_id = (entry or {}).get("config", {}).get("provider_id", "") if entry else ""
    except Exception:  # noqa: BLE001 - a registry hiccup must never block a request
        usage_engine, usage_model_id, provider_id = "", "", ""
    # Read the identity HERE: the background job outlives the request, and the
    # Request object must not be touched from inside it.
    caller_id = current_user_id(request) or ""
    try:
        await quota_gate(
            user_id=caller_id, provider_id=provider_id,
            kind="tts", engine=usage_engine, model_id=usage_model_id,
        )
    except QuotaExceededError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
```

- [ ] **Step 4: Meter each chunk**

Inside the job's `_run`, right after each `result = await provider.synthesize(chunk_request)`:

```python
                try:
                    await record_usage(
                        user_id=caller_id, profile_id="",
                        kind="tts", engine=payload.engine, model_id=payload.model_id or "",
                        unit="chars", native_amount=len(segment or ""),
                    )
                except Exception as exc:  # noqa: BLE001 - metering must never break the job
                    logger.warning("tts stream usage metering failed: %s", exc)
```

`record_usage` is already imported in this module. Note it records `payload.engine` rather than the resolved `usage_engine`: the resolver runs again inside `record_usage`, and passing the raw request values keeps this call identical in shape to the `/synthesize` one.

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/unit/test_tts_stream_metering.py -q` then `.venv/bin/python -m pytest tests/unit -q -k tts`
Expected: PASS both. Report counts.

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/api/routes/tts.py tests/unit/test_tts_stream_metering.py
git commit -m "fix(usage): give the TTS stream job an identity, a gate, and metering"
```

---

## Phase B — make a future gap fail CI

### Task 3: The paid-call-site inventory test

**Files:**
- Create: `tests/unit/test_paid_call_site_inventory.py`

**Interfaces:**
- Produces: a test that scans `apps/api_gateway/app` for provider-invoking calls and compares what it finds against an explicit table. Nothing imports it; it is a gate.

**What it catches, precisely — and what it does not.** Be honest about both in the module docstring:
- Catches a **new file** that calls a provider, a **new provider-invoking method name** used anywhere, and **an additional call added to an already-classified file** (the count is part of the key).
- Catches a classification whose `covering_test` names a test that does not exist.
- Does **not** catch a caller that reaches a provider through a new indirection the regex does not know about, and does not verify that a site marked `metered` truly meters — Task 4 is what checks behavior. The inventory makes an omission *loud*; it cannot make it impossible.

- [ ] **Step 1: Write the test**

Create `tests/unit/test_paid_call_site_inventory.py`:

```python
"""Every call that can spend money is classified here, on purpose.

Three times in this subsystem's history a paid call site was added and nobody
metered it: the /chat LLM path, the whole livehost endpoint, and both streaming
endpoints. Each was found months later by an audit. This test replaces the audit:
a new provider-invoking call fails it until someone adds a row below, with a
status, a reason, and the name of a test that covers the behavior.

WHAT THIS CATCHES: a new file calling a provider; a new provider-invoking method
name; an extra call added to an already-listed file (the count is part of the
key); a row naming a covering test that does not exist.

WHAT IT DOES NOT: it cannot tell whether a site marked "metered" really records a
row -- that is test_every_paid_entry_point_records_usage. And a caller reaching a
provider through an indirection these patterns do not match would slip past. This
makes an omission loud; it does not make one impossible.
"""

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "apps" / "api_gateway" / "app"

# The methods that actually reach a provider (network or local inference).
_CALL_PATTERN = re.compile(
    r"(?<![\w.])(?:await\s+)?[\w.\[\]()]*\.?"
    r"(transcribe_bytes|synthesize|reply_stream|reply|open_stream"
    r"|embed_texts|embed_texts_with_usage)\s*\("
)

# Files that DEFINE these methods rather than call a provider through them.
_IMPLEMENTATIONS = (
    "services/stt/providers/",
    "services/tts/providers/",
    "services/conversation/responder.py",
    "services/memory/embedder.py",
)

# (relative path, method) -> (call count, status, reason, covering test)
#
# status is one of:
#   "metered+gated"  -- records usage and checks the quota itself
#   "covered-by-caller" -- a helper; its caller records one row for the whole unit
#   "exempt" -- deliberately unmetered or ungated, with the reason stated
_CLASSIFIED: dict[tuple[str, str], tuple[int, str, str, str]] = {
    ("api/routes/conversation.py", "reply_stream"): (
        1, "metered+gated", "POST /v1/conversation/chat, tool-enabled path",
        "tests/unit/test_routes_usage_metering.py",
    ),
    ("api/routes/conversation.py", "reply"): (
        1, "metered+gated", "POST /v1/conversation/chat, plain path",
        "tests/unit/test_routes_usage_metering.py",
    ),
    ("api/routes/stt.py", "transcribe_bytes"): (
        1, "metered+gated", "POST /v1/stt/transcribe",
        "tests/unit/test_routes_usage_metering.py",
    ),
    ("api/routes/stt.py", "open_stream"): (
        1, "metered+gated", "WS /v1/stt/stream: gated at connect and each flush",
        "tests/unit/test_stt_stream_metering.py",
    ),
    ("api/routes/tts.py", "synthesize"): (
        2, "metered+gated", "POST /v1/tts/synthesize and the /v1/tts/stream job",
        "tests/unit/test_tts_stream_metering.py",
    ),
    ("api/routes/livehost.py", "transcribe_bytes"): (
        1, "metered+gated", "livehost voice turn STT",
        "tests/unit/test_livehost_quota_gate.py",
    ),
    ("api/routes/livehost.py", "synthesize"): (
        1, "metered+gated", "livehost TTS per sentence",
        "tests/unit/test_livehost_quota_gate.py",
    ),
    ("api/routes/livehost.py", "reply_stream"): (
        2, "metered+gated", "livehost voice and social turns",
        "tests/unit/test_livehost_quota_gate.py",
    ),
    ("services/conversation/session.py", "transcribe_bytes"): (
        1, "metered+gated", "conversation core STT, incl. the fast-path engine switch",
        "tests/unit/test_session_usage_metering.py",
    ),
    ("services/conversation/session.py", "synthesize"): (
        2, "metered+gated", "conversation core TTS, prefetch and direct",
        "tests/unit/test_session_usage_metering.py",
    ),
    ("services/conversation/session.py", "reply_stream"): (
        2, "metered+gated", "conversation core LLM, tool and plain paths",
        "tests/unit/test_session_usage_metering.py",
    ),
    ("services/memory/extractor.py", "embed_texts_with_usage"): (
        1, "metered+gated", "memory fact embedding at session teardown",
        "tests/unit/test_memory_usage_metering.py",
    ),
    ("services/memory/retriever.py", "embed_texts_with_usage"): (
        1, "metered+gated", "per-turn query embedding; gated by the turn it runs in",
        "tests/unit/test_memory_usage_metering.py",
    ),
    ("services/stt/segmented.py", "transcribe_bytes"): (
        2, "covered-by-caller",
        "long-clip segments; the route records one row for the whole clip, so "
        "metering here would double-count",
        "tests/unit/test_routes_usage_metering.py",
    ),
    ("services/stt/base.py", "transcribe_bytes"): (
        1, "covered-by-caller",
        "streaming adapter reached only from WS /v1/stt/stream, which meters per flush",
        "tests/unit/test_stt_stream_metering.py",
    ),
    ("services/stt/streaming_chunked.py", "transcribe_bytes"): (
        1, "covered-by-caller",
        "chunked streaming adapter, same path as services/stt/base.py",
        "tests/unit/test_stt_stream_metering.py",
    ),
    ("api/routes/model_registry.py", "transcribe_bytes"): (
        1, "exempt",
        "add-time credential test; metered but never gated, or an admin over "
        "quota could not validate the provider needed to fix it",
        "tests/unit/test_model_registry_test_call_metering.py",
    ),
    ("api/routes/model_registry.py", "synthesize"): (
        1, "exempt", "add-time credential test; see transcribe_bytes above",
        "tests/unit/test_model_registry_test_call_metering.py",
    ),
    ("api/routes/model_registry.py", "reply"): (
        1, "exempt", "add-time credential test; see transcribe_bytes above",
        "tests/unit/test_model_registry_test_call_metering.py",
    ),
    ("api/routes/model_registry.py", "embed_texts"): (
        2, "exempt",
        "add-time credential test (import line plus the call); see transcribe_bytes",
        "tests/unit/test_model_registry_test_call_metering.py",
    ),
}

_VALID_STATUSES = {"metered+gated", "covered-by-caller", "exempt"}


def _found_call_sites() -> dict[tuple[str, str], int]:
    found: dict[tuple[str, str], int] = {}
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP).as_posix()
        if "__pycache__" in rel or any(rel.startswith(p) for p in _IMPLEMENTATIONS):
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            # Skip definitions and comments -- we want callers, not declarations.
            if stripped.startswith(("def ", "async def ", "#")):
                continue
            for match in _CALL_PATTERN.finditer(line):
                found[(rel, match.group(1))] = found.get((rel, match.group(1)), 0) + 1
    return found


def test_every_paid_call_site_is_classified():
    found = _found_call_sites()
    unclassified = sorted(set(found) - set(_CLASSIFIED))
    assert not unclassified, (
        "New paid call site(s) found. Add each to _CLASSIFIED with a status, a "
        "reason, and a covering test -- and make sure the site actually meters "
        f"and gates before you call it metered:\n  " + "\n  ".join(map(str, unclassified))
    )


def test_no_classified_call_site_has_disappeared():
    """A stale row hides the fact that nobody is checking that path any more."""
    found = _found_call_sites()
    gone = sorted(set(_CLASSIFIED) - set(found))
    assert not gone, (
        "These classified call sites no longer exist. Remove their rows so the "
        f"table keeps describing the real code:\n  " + "\n  ".join(map(str, gone))
    )


def test_call_counts_match_so_an_added_call_cannot_hide():
    """Keying on (file, method) alone would let a second, unmetered call slip
    into a file that already has a classified one."""
    found = _found_call_sites()
    drifted = [
        f"{key}: classified {_CLASSIFIED[key][0]}, found {count}"
        for key, count in sorted(found.items())
        if key in _CLASSIFIED and _CLASSIFIED[key][0] != count
    ]
    assert not drifted, (
        "Call count changed. If you added a call, meter and gate it, then update "
        f"the count:\n  " + "\n  ".join(drifted)
    )


def test_every_classification_names_a_test_that_exists():
    """A row claiming coverage from a test that does not exist is worse than no
    row at all: it reads as proof."""
    repo_root = Path(__file__).resolve().parents[2]
    missing = []
    for key, (_count, status, reason, covering_test) in sorted(_CLASSIFIED.items()):
        assert status in _VALID_STATUSES, f"{key}: unknown status {status!r}"
        assert reason.strip(), f"{key}: a classification needs a reason"
        if not (repo_root / covering_test).is_file():
            missing.append(f"{key}: {covering_test}")
    assert not missing, "Covering test file(s) do not exist:\n  " + "\n  ".join(missing)
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest tests/unit/test_paid_call_site_inventory.py -q`
Expected: PASS — the table above was built from the real inventory after Tasks 1-2, except that `tests/unit/test_model_registry_test_call_metering.py` does not exist yet, so `test_every_classification_names_a_test_that_exists` FAILS. That failure is correct and Task 5 creates the file. Report the failure and proceed; do not weaken the assertion.

If a count or a key does not match reality, the source is the truth: fix the TABLE, and report each correction. A mismatch is exactly what this test is for.

- [ ] **Step 3: Prove the test can fail**

Add a temporary unmetered call to a file — e.g. a bare `await provider.synthesize(payload)` line in `apps/api_gateway/app/api/routes/health.py` — and confirm `test_every_paid_call_site_is_classified` fails naming it. Then remove the temporary line and confirm the test passes again. Quote both outputs in your report. **A gate nobody has seen fail is not a gate.**

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_paid_call_site_inventory.py
git commit -m "test(usage): fail CI when a paid call site is added without classification"
```

---

### Task 4: The behavioral end-to-end metering test

**Files:**
- Create: `tests/unit/test_every_paid_entry_point_meters.py`

**Interfaces:**
- Consumes: the four REST/WS entry points and their stub-provider patterns.
- Produces: one test per externally reachable paid entry point, each asserting a real row in `usage_events`.

**Why this exists next to Task 3:** the inventory test proves someone *classified* every call site; this one proves the classification is *true* for every entry point a client can reach. A stub provider cannot satisfy it — the assertion reads the database.

- [ ] **Step 1: Write the test**

Create `tests/unit/test_every_paid_entry_point_meters.py`:

```python
"""One test per externally reachable paid entry point, each asserting a real row
lands in usage_events.

test_paid_call_site_inventory.py proves every call site was classified; this
proves the classification is true where a client can reach it. Both are needed:
the first catches an omission, the second catches a lie.

Deliberately NOT covered here (each has its own dedicated suite, named so a
reader can check): the conversation core (tests/unit/test_session_usage_metering.py),
livehost (tests/unit/test_livehost_quota_gate.py), and the memory subsystem
(tests/unit/test_memory_usage_metering.py). Those run over a WebSocket or a
session teardown that this file's harness cannot drive.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.audio import pcm16_to_wav_bytes
from app.main import app
from app.schemas.stt import STTResult
from app.schemas.tts import TTSResult
from app.services.db.engine import db_session, init_db
from app.services.db.models import UsageEvent
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service


class _StubSTT(STTProvider):
    name = "stub-e2e-stt"

    def available(self) -> bool:
        return True

    async def transcribe_bytes(self, audio: bytes, language=None, model=None) -> STTResult:
        return STTResult(text="xin chao", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-e2e-tts"

    def available(self) -> bool:
        return True

    async def synthesize(self, request) -> TTSResult:
        return TTSResult(audio_url="/artifacts/x.wav", duration_seconds=0.1,
                         sample_rate=16000, engine=self.name)


@pytest.fixture
def _stubs():
    stt_service.providers["stub-e2e-stt"] = _StubSTT()
    tts_service.providers["stub-e2e-tts"] = _StubTTS()
    yield
    stt_service.providers.pop("stub-e2e-stt", None)
    tts_service.providers.pop("stub-e2e-tts", None)


async def _rows():
    async with db_session() as s:
        return list((await s.execute(select(UsageEvent))).scalars().all())


async def _wait_for_rows(minimum: int = 1, tries: int = 50) -> list:
    for _ in range(tries):
        rows = await _rows()
        if len(rows) >= minimum:
            return rows
        await asyncio.sleep(0.02)
    return await _rows()


async def test_transcribe_meters(_stubs):
    await init_db()
    client = TestClient(app)
    wav = pcm16_to_wav_bytes(b"\x00\x00" * 1600, sample_rate=16000)
    resp = client.post("/v1/stt/transcribe", files={"audio": ("a.wav", wav, "audio/wav")},
                       data={"engine": "stub-e2e-stt"})
    assert resp.status_code == 200, resp.text
    stt = [r for r in await _rows() if r.kind == "stt"]
    assert len(stt) == 1 and stt[0].native_amount > 0


async def test_synthesize_meters(_stubs):
    await init_db()
    client = TestClient(app)
    resp = client.post("/v1/tts/synthesize", json={"text": "xin chao", "engine": "stub-e2e-tts"})
    assert resp.status_code == 200, resp.text
    tts = [r for r in await _rows() if r.kind == "tts"]
    assert len(tts) == 1 and tts[0].native_amount == len("xin chao")


async def test_tts_stream_job_meters(_stubs):
    await init_db()
    client = TestClient(app)
    resp = client.post("/v1/tts/stream", json={"text": "xin chao", "engine": "stub-e2e-tts"})
    assert resp.status_code == 200, resp.text
    await _wait_for_rows(1)
    tts = [r for r in await _rows() if r.kind == "tts"]
    assert tts, "the background stream job recorded nothing"


async def test_stt_stream_socket_meters(_stubs):
    import json

    await init_db()
    client = TestClient(app)
    with client.websocket_connect(
        "/v1/stt/stream?engine=stub-e2e-stt&sample_rate=16000&denoise=false&vad=false"
    ) as ws:
        assert ws.receive_json()["event_type"] == "session_started"
        ws.send_bytes(b"\x00\x00" * 1600)
        ws.send_text(json.dumps({"type": "end"}))
        for _ in range(5):
            if ws.receive_json()["event_type"] == "final":
                break
    stt = [r for r in await _rows() if r.kind == "stt"]
    assert stt, "the streaming socket recorded nothing"


async def test_chat_meters():
    await init_db()
    client = TestClient(app)
    resp = client.post("/v1/conversation/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200, resp.text
    llm = [r for r in await _rows() if r.kind == "llm"]
    assert len(llm) == 1
```

Note: `_StubSTT` has no `open_stream`, so the WS test exercises the base-class streaming adapter — which is the point, since that adapter is one of the `covered-by-caller` rows in the inventory. If `STTProvider`'s default `open_stream` cannot be driven this way, register a stub with an explicit stream (as `tests/unit/test_stt_stream_metering.py` does) and note the change.

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest tests/unit/test_every_paid_entry_point_meters.py -q`
Expected: PASS (Tasks 1-2 made the two stream cases work). If any fails, that is a real gap — report it rather than adjusting the assertion.

- [ ] **Step 3: Prove each test can fail**

For ONE of the five (pick `test_synthesize_meters`), comment out the `record_usage` call in `apps/api_gateway/app/api/routes/tts.py`'s `/synthesize` handler, confirm the test fails, then restore it. Quote both outputs. Confirm the restored file matches its committed state with `git diff --exit-code apps/api_gateway/app/api/routes/tts.py`.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_every_paid_entry_point_meters.py
git commit -m "test(usage): drive every paid entry point and assert a real usage row"
```

---

## Phase C — the remaining correctness items

### Task 5: Meter the Model Registry's add-time provider calls

**Files:**
- Modify: `apps/api_gateway/app/api/routes/model_registry.py` (`create_entry`)
- Test: `tests/unit/test_model_registry_test_call_metering.py`

**Interfaces:**
- Consumes: `record_usage`, `current_user_id`.
- Produces: the file `tests/unit/test_model_registry_test_call_metering.py`, which Task 3's inventory table already names as the covering test for the four `exempt` rows — Task 3's fourth assertion fails until this exists.

**The decision this task implements, and its rationale.** The add-time test call is a real provider call that really costs money. It is **metered** so the spend is visible, and deliberately **not gated**: an admin whose quota is exhausted must still be able to validate the credentials of the provider they need in order to fix the configuration that exhausted it. Gating here would lock them out of their own recovery path. Record it with `request_id="registry-test-call"` so it is distinguishable from serving traffic in a query.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_model_registry_test_call_metering.py`:

```python
"""The Model Registry's add-time test call really hits the provider and really
costs money. It is metered so the spend is visible, and deliberately NOT gated:
an admin over quota must still be able to validate the provider they need in
order to fix the config that put them over."""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.settings import settings
from app.main import app
from app.services.db.engine import db_session, init_db
from app.services.db.models import UsageEvent
from app.services.model_registry.store import model_registry_store
from app.services.quota.store import quota_store
from app.services.usage.recorder import record_usage


@pytest.fixture
def _with_admin(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    client = TestClient(app)
    client.post("/api/auth/signup", json={"username": "regadm", "password": "pw"})
    from app.services.auth.users import user_store
    user = asyncio.run(user_store.get_by_username("regadm"))
    asyncio.run(user_store.set_fields(user.id, role="admin"))
    client.post("/api/auth/login", json={"username": "regadm", "password": "pw"})
    yield client
    monkeypatch.setattr(settings, "admin_password", "")


@pytest.fixture
def _fake_llm(monkeypatch):
    async def fake_post(self, url, headers=None, json=None):
        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": "ok"}}],
                        "usage": {"prompt_tokens": 7, "completion_tokens": 3}}

        return R()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


async def _rows():
    async with db_session() as s:
        return list((await s.execute(select(UsageEvent))).scalars().all())


def test_the_add_time_test_call_is_metered(_with_admin, _fake_llm):
    resp = _with_admin.post("/v1/model_registry", json={
        "kind": "llm", "engine": "OA", "model_id": "gpt-4o-mini", "label": "OA mini",
        "base_url": "http://llm.local/v1", "api_key": "k",
    })
    assert resp.status_code == 200, resp.text
    rows = asyncio.run(_rows())
    metered = [r for r in rows if r.request_id == "registry-test-call"]
    assert len(metered) == 1, f"the add-time provider call was not metered: {rows}"
    assert metered[0].kind == "llm" and metered[0].model_id == "gpt-4o-mini"


def test_an_over_quota_admin_can_still_validate_a_provider(_with_admin, _fake_llm):
    """The recovery path: gating this call would trap an admin whose quota is
    exhausted, unable to test the credentials needed to fix it."""
    asyncio.run(init_db())
    quota_store.invalidate()
    asyncio.run(model_registry_store.create(
        "llm", "priced-eng", "priced-model", "Priced",
        config={"price": {"unit": "1M_tokens", "in": 100.0}},
    ))
    asyncio.run(record_usage(user_id="", profile_id="", kind="llm", engine="priced-eng",
                             model_id="priced-model", unit="tokens",
                             native_amount=1_000_000, prompt_tokens=1_000_000))
    asyncio.run(quota_store.create(scope="global", scope_id="", limit_usd=1.0, period="monthly"))

    resp = _with_admin.post("/v1/model_registry", json={
        "kind": "llm", "engine": "OA2", "model_id": "gpt-4o-mini", "label": "OA mini 2",
        "base_url": "http://llm.local/v1", "api_key": "k",
    })
    assert resp.status_code == 200, f"an over-quota admin must still be able to test: {resp.text}"
```

- [ ] **Step 2: Run it to see it fail**

Run: `.venv/bin/python -m pytest tests/unit/test_model_registry_test_call_metering.py -q`
Expected: the first test fails (no metered row); the second already passes (nothing gates that route today) — say so in your report rather than presenting it as proof of your change.

- [ ] **Step 3: Meter the test call**

In `create_entry`, after the whole `try/except` block that performs the kind-specific provider call succeeds and BEFORE `model_registry_store.create(...)`:

```python
    # The add-time test call above really hit the provider, so it really cost
    # money -- record it. Deliberately NOT gated: an admin over quota must still
    # be able to validate the provider they need in order to fix the config that
    # put them over. request_id marks it as validation, not serving traffic.
    try:
        await record_usage(
            user_id=current_user_id(request) or "", profile_id="",
            kind=payload.kind, engine=payload.engine, model_id=payload.model_id,
            unit={"llm": "tokens", "embed": "tokens", "stt": "seconds", "tts": "chars"}
                 .get(payload.kind, ""),
            native_amount=0.0, request_id="registry-test-call",
        )
    except Exception as exc:  # noqa: BLE001 - metering must never break an admin action
        logger.warning("registry test-call metering failed: %s", exc)
```

`create_entry` does not currently take a `Request`. Add `request: Request` to its signature (FastAPI injects it; no caller changes) and import `Request` from `fastapi` and `current_user_id` from `app.core.actor` if they are not already imported. Check first — `logger` may also need defining in this module; if there is no module-level logger, add one following the pattern in `routes/stt.py`.

`native_amount=0.0` is deliberate: the test call's real token count is not worth threading out of four different provider shapes, and a zero-amount row still records that the call happened, by whom, and against which model. State this in your report so a reader does not mistake it for an oversight.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/unit/test_model_registry_test_call_metering.py tests/unit/test_paid_call_site_inventory.py -q` then `.venv/bin/python -m pytest tests/unit -q -k model_registry`
Expected: PASS all — including Task 3's fourth assertion, which was failing only because this file did not exist.

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/api/routes/model_registry.py tests/unit/test_model_registry_test_call_metering.py
git commit -m "feat(usage): meter the registry add-time provider call, never gate it"
```

---

### Task 6: The three small correctness items

**Files:**
- Modify: `apps/api_gateway/app/api/routes/stt.py` and `apps/api_gateway/app/api/routes/tts.py` (pass `profile_id`)
- Modify: `apps/api_gateway/app/api/routes/conversation.py` (move the resolver inside the guard)
- Modify: `apps/api_gateway/app/static/js/pricing.js` (hide sentinel rows)
- Test: `tests/unit/test_routes_usage_metering.py` (append)

**Interfaces:** no API changes.

**The three items:**
1. **REST metering records `profile_id=""`.** Both routes accept a `profile` query parameter elsewhere in the app; `/v1/stt/transcribe` and `/v1/tts/synthesize` do not. Add an optional `profile: str | None = None` query parameter to each and pass it as `profile_id`, so the profile dimension of the dashboards has data from REST too, not only from the conversation core.
2. **`conversation.py`'s `/chat` gate calls `resolve_usage_model` outside its guarded `try`** — the only one of six that does. Move it inside, with the `except` degrading engine, model AND `provider_id` to blanks.
3. **The Pricing tab lists engine-config sentinel rows** (`model_id == ""`, shown as "(engine config)") as priceable. Attribution never resolves to a sentinel, so a price set there can never match a usage row: it is a setting that silently does nothing. Filter them out of the table.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_routes_usage_metering.py`:

```python
async def test_rest_metering_records_the_profile_when_given_one():
    """Without this the profile dimension of the dashboards only ever has data
    from the conversation core, so a REST-driven integration is invisible in it."""
    stt_service.providers["stub-prof-stt"] = _StubSTT()
    try:
        client = TestClient(app)
        wav = pcm16_to_wav_bytes(b"\x00\x00" * 1600, sample_rate=16000)
        resp = client.post(
            "/v1/stt/transcribe?profile=my-profile",
            files={"audio": ("a.wav", wav, "audio/wav")},
            data={"engine": "stub-prof-stt"},
        )
        assert resp.status_code == 200, resp.text
        stt = [r for r in await _rows() if r.kind == "stt"]
        assert len(stt) == 1
        assert stt[0].profile_id == "my-profile"
    finally:
        stt_service.providers.pop("stub-prof-stt", None)


async def test_synthesize_records_the_profile_when_given_one():
    tts_service.providers["stub-prof-tts"] = _StubTTS()
    try:
        client = TestClient(app)
        resp = client.post(
            "/v1/tts/synthesize?profile=my-profile",
            json={"text": "xin chao", "engine": "stub-prof-tts"},
        )
        assert resp.status_code == 200, resp.text
        tts = [r for r in await _rows() if r.kind == "tts"]
        assert len(tts) == 1
        assert tts[0].profile_id == "my-profile"
    finally:
        tts_service.providers.pop("stub-prof-tts", None)
```

Reuse the `_StubSTT`, `_StubTTS`, `_rows` and `pcm16_to_wav_bytes` already defined/imported at the top of that file — read it first and do not redefine them. If its stub classes are named differently, use the real names.

- [ ] **Step 2: Run it to see it fail**

Run: `.venv/bin/python -m pytest tests/unit/test_routes_usage_metering.py -q`
Expected: the two new tests fail — `profile_id` is `""`.

- [ ] **Step 3: Thread the profile through both routes**

Add `profile: str | None = None` to the signature of `transcribe` in `routes/stt.py` and `synthesize` in `routes/tts.py` (as a query parameter — put it next to the other non-body parameters), and change each `record_usage` call's `profile_id=""` to `profile_id=profile or ""`.

- [ ] **Step 4: Move the `/chat` resolver inside its guard**

In `routes/conversation.py`, the `/chat` pre-flight currently calls `resolve_usage_model` before the `try` that guards the registry lookup. Move the call inside that `try`, and make the `except` reset all three values:

```python
    quota_engine, quota_model_id = "", ""
    provider_id = ""
    try:
        quota_engine, quota_model_id = await resolve_usage_model(
            "llm",
            (active_profile.llm.engine if active_profile and pinned_model else "") or "",
            pinned_model,
        )
        entry = await model_registry_store.find("llm", quota_engine, quota_model_id)
        provider_id = (entry or {}).get("config", {}).get("provider_id", "") if entry else ""
    except Exception:  # noqa: BLE001 - a lookup failure must never block the request
        quota_engine, quota_model_id, provider_id = "", "", ""
```

Read that block first: it already computes a `pinned_model` for the engine-pairing rule. Preserve that rule exactly — this task only moves the call inside the guard, it must not change which engine/model pair is resolved.

- [ ] **Step 5: Hide sentinel rows from the Pricing tab**

In `apps/api_gateway/app/static/js/pricing.js`, `_render` filters rows by `RATE_KEYS[r.unit]`. Add a second filter dropping sentinel rows, with the reason:

```javascript
    // Engine-config sentinel rows (model_id === "") can never match a usage row:
    // attribution never resolves to a sentinel, so a price set here would
    // silently never apply. Don't offer it.
    .filter((r) => r.model_id)
```

Then remove the now-unreachable `|| "(engine config)"` fallback in `_renderRow`, since no row reaching it can have a blank `model_id`.

- [ ] **Step 6: Verify**

```bash
.venv/bin/python -m pytest tests/unit/test_routes_usage_metering.py tests/unit/test_quota_provider_scope.py -q
node --check apps/api_gateway/app/static/js/pricing.js
grep -nE '[‘’“”]' apps/api_gateway/app/static/js/pricing.js || echo "no smart quotes"
```
Expected: tests PASS, `node --check` silent, no smart quotes. Then read the changed `pricing.js` region back.

- [ ] **Step 7: Commit**

```bash
git add apps/api_gateway/app/api/routes/stt.py apps/api_gateway/app/api/routes/tts.py apps/api_gateway/app/api/routes/conversation.py apps/api_gateway/app/static/js/pricing.js tests/unit/test_routes_usage_metering.py
git commit -m "fix(usage): attribute REST usage to a profile, guard the chat resolver, hide sentinel rows from Pricing"
```

---

## Phase D — make enforcement legible

### Task 7: Show spend against limit

**Files:**
- Modify: `apps/api_gateway/app/api/routes/quotas.py` (add spend to the list response)
- Modify: `apps/api_gateway/app/static/js/quotas.js` (a Spend column)
- Modify: `apps/api_gateway/app/api/routes/usage.py` (`/v1/usage/me` returns the caller's applicable limits)
- Modify: `apps/api_gateway/app/static/js/usage-me.js` (show them)
- Modify: `apps/api_gateway/app/static/index.html` (My Usage hint)
- Test: `tests/unit/test_quotas_routes.py` and `tests/unit/test_usage_routes.py` (append)

**Interfaces:**
- `GET /v1/quotas` rows gain `spend_usd: float` — the current spend for that row's scope and period, from `current_spend`.
- `GET /v1/usage/me` response becomes `{"success": true, "data": [...], "limits": [{"scope", "limit_usd", "spend_usd", "period"}]}`. `data` keeps its exact current shape so the React client (`lugo-web-client/src/api/usage.ts`) is unaffected.

**Why:** a user who hits 429 currently has no way to see why, and an admin cannot tell how close a quota is to biting without doing arithmetic against the Usage tab. The numbers are correct now; showing them is what makes enforcement legible.

**Only the caller's own limits on `/v1/usage/me`:** the global quota and their own user quota. Never another user's, and never a provider quota's absolute spend — that is cross-tenant information on an endpoint every logged-in user can reach.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_quotas_routes.py`:

```python
def test_quota_list_includes_current_spend(client, _with_password):
    """An admin cannot judge a limit without seeing what has been spent against it."""
    import asyncio

    from app.services.db.engine import init_db
    from app.services.model_registry.store import model_registry_store
    from app.services.usage.recorder import record_usage

    _login_admin(client, "q-spend")
    asyncio.run(init_db())
    asyncio.run(model_registry_store.create(
        "llm", "spend-eng", "spend-model", "Spend",
        config={"price": {"unit": "1M_tokens", "in": 3.0}},
    ))
    asyncio.run(record_usage(user_id="u-spend", profile_id="", kind="llm", engine="spend-eng",
                             model_id="spend-model", unit="tokens",
                             native_amount=1_000_000, prompt_tokens=1_000_000))  # $3
    created = client.post("/v1/quotas", json={
        "scope": "user", "scope_id": "u-spend", "limit_usd": 10.0, "period": "monthly",
    }).json()["data"]

    row = next(q for q in client.get("/v1/quotas").json()["data"] if q["id"] == created["id"])
    assert abs(row["spend_usd"] - 3.0) < 1e-9
    assert row["limit_usd"] == 10.0
```

Append to `tests/unit/test_usage_routes.py`:

```python
def test_my_usage_reports_the_callers_own_limits(client, _with_password):
    """A user who gets a 429 must be able to see why. Their own user quota and
    the global one -- never another user's, never a provider's."""
    import asyncio

    from app.services.auth.users import user_store
    from app.services.quota.store import quota_store

    _signup_login(client, "limit-viewer")
    me = asyncio.run(user_store.get_by_username("limit-viewer"))
    asyncio.run(init_db())
    quota_store.invalidate()
    asyncio.run(quota_store.create(scope="user", scope_id=me.id, limit_usd=5.0, period="monthly"))
    asyncio.run(quota_store.create(scope="global", scope_id="", limit_usd=50.0, period="monthly"))
    asyncio.run(quota_store.create(scope="user", scope_id="someone-else", limit_usd=1.0,
                                   period="monthly"))
    asyncio.run(quota_store.create(scope="provider", scope_id="prov-x", limit_usd=2.0,
                                   period="monthly"))

    body = client.get("/v1/usage/me").json()
    scopes = sorted((l["scope"], l["limit_usd"]) for l in body["limits"])
    assert scopes == [("global", 50.0), ("user", 5.0)], f"leaked or missing limits: {body['limits']}"
    assert all("spend_usd" in l for l in body["limits"])
    # The existing shape must not change -- the React client reads `data`.
    assert isinstance(body["data"], list)
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest tests/unit/test_quotas_routes.py tests/unit/test_usage_routes.py -q`
Expected: both new tests fail with `KeyError: 'spend_usd'` / `KeyError: 'limits'`.

- [ ] **Step 3: Add spend to the quota list**

In `apps/api_gateway/app/api/routes/quotas.py`:

```python
@router.get("")
async def list_quotas() -> dict:
    """Each row carries the spend already counted against it -- a limit without
    its spend cannot be judged."""
    from app.services.quota.gate import current_spend

    rows = await quota_store.list_all()
    for row in rows:
        try:
            row["spend_usd"] = await current_spend(
                scope=row["scope"], scope_id=row["scope_id"], period=row["period"]
            )
        except Exception:  # noqa: BLE001 - a spend read must never break the list
            row["spend_usd"] = 0.0
    return {"success": True, "data": rows}
```

- [ ] **Step 4: Return the caller's limits from `/v1/usage/me`**

In `apps/api_gateway/app/api/routes/usage.py`:

```python
async def _limits_for(user_id: str) -> list[dict]:
    """The quotas that can block THIS caller: their own user quota and the
    global one. Never another user's, and never a provider quota -- its spend is
    cross-tenant information, and this endpoint is open to every logged-in user.
    """
    from app.services.quota.gate import current_spend
    from app.services.quota.store import quota_store

    out = []
    try:
        for quota in await quota_store.list_enabled():
            if quota["scope"] == "global" or (
                quota["scope"] == "user" and quota["scope_id"] == user_id
            ):
                out.append({
                    "scope": quota["scope"],
                    "period": quota["period"],
                    "limit_usd": quota["limit_usd"],
                    "spend_usd": await current_spend(
                        scope=quota["scope"], scope_id=quota["scope_id"], period=quota["period"]
                    ),
                })
    except Exception as exc:  # noqa: BLE001 - never break the usage view over this
        logger.warning("reading own limits failed: %s", exc)
    return out
```

and in `get_my_usage`, return `{"success": True, "data": data, "limits": await _limits_for(user_id)}`. Add a module-level `logger = logging.getLogger(__name__)` if the file has none.

- [ ] **Step 5: Show spend in the Quotas table**

In `apps/api_gateway/app/static/js/quotas.js`, add a column after `limit_usd`:

```javascript
      {
        key: "spend_usd",
        label: "Spent",
        render: (q) => {
          const spent = Number(q.spend_usd || 0);
          const limit = Number(q.limit_usd || 0);
          const pct = limit > 0 ? Math.round((spent / limit) * 100) : 0;
          const over = limit > 0 && spent >= limit;
          return `<span class="${over ? "danger" : ""}">$${spent.toFixed(4)} (${pct}%)</span>`;
        },
      },
```

- [ ] **Step 6: Show the limits in My Usage**

In `apps/api_gateway/app/static/js/usage-me.js`, keep `_render(host, body.data || [])` for the table and add a line above it summarizing the limits. Pass `body.limits` into `loadMyUsage`'s render step and prepend:

```javascript
function _renderLimits(limits) {
  if (!limits || !limits.length) return "";
  const parts = limits.map((l) => {
    const spent = Number(l.spend_usd || 0);
    const limit = Number(l.limit_usd || 0);
    const over = limit > 0 && spent >= limit;
    const label = l.scope === "global" ? "Shared limit" : "Your limit";
    return `<li class="${over ? "danger" : ""}">${label} (${escapeHtml(String(l.period))}): $${spent.toFixed(4)} of $${limit.toFixed(2)}${over ? " - reached" : ""}</li>`;
  });
  return `<ul class="limit-list">${parts.join("")}</ul>`;
}
```

and render it into the host before the table (`host.innerHTML = _renderLimits(limits) + tableHtml`). Read the current `_render` first and integrate rather than duplicating it.

Also append one sentence to the My Usage hint in `index.html`: `A limit shown as reached means new requests are refused until the period rolls over or the limit is raised.`

- [ ] **Step 7: Verify**

```bash
.venv/bin/python -m pytest tests/unit/test_quotas_routes.py tests/unit/test_usage_routes.py -q
node --check apps/api_gateway/app/static/js/quotas.js
node --check apps/api_gateway/app/static/js/usage-me.js
grep -nE '[‘’“”]' apps/api_gateway/app/static/js/quotas.js apps/api_gateway/app/static/js/usage-me.js apps/api_gateway/app/static/index.html || echo "no smart quotes"
```
Expected: PASS, silent, no smart quotes. Read both changed JS regions back.

- [ ] **Step 8: Commit**

```bash
git add apps/api_gateway/app/api/routes/quotas.py apps/api_gateway/app/api/routes/usage.py apps/api_gateway/app/static/js/quotas.js apps/api_gateway/app/static/js/usage-me.js apps/api_gateway/app/static/index.html tests/unit/test_quotas_routes.py tests/unit/test_usage_routes.py
git commit -m "feat(quota): show spend against limit in Quotas and My Usage"
```

---

### Task 8: Say "quota exceeded" instead of a generic error

**Files:**
- Modify: `apps/api_gateway/app/static/js/helpers.js` (a shared 429 formatter)
- Modify: `apps/api_gateway/app/static/js/stt-batch.js`, `tts-batch.js` (use it)
- Modify: `lugo-web-client/src/api/client.ts` (a typed quota error)
- Test: `lugo-web-client/src/api/client.test.ts` (append)

**Interfaces:**
- Produces: `quotaMessage(resp, body)` in `helpers.js` — returns the quota detail when `resp.status === 429`, else `""`. And `QuotaExceededError` exported from the React api client, thrown on any 429 so screens can render it distinctly.

**Why:** the server already returns a precise message (`user quota exceeded for u1: $12.0400 / $12.0000 (monthly)`). Every client currently drops it into a generic error line, so the one thing the user needs to know — that they are out of budget, not that something broke — is the thing they do not see.

- [ ] **Step 1: Write the failing React test**

Append to `lugo-web-client/src/api/client.test.ts` (read the file first for its mocking style):

```ts
it('throws a QuotaExceededError on 429 so screens can tell budget from breakage', async () => {
  const detail = 'user quota exceeded for u1: $12.0400 / $12.0000 (monthly)'
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    new Response(JSON.stringify({ detail }), { status: 429 }) as unknown as Response,
  )
  await expect(apiFetch('/v1/tts/synthesize', { method: 'POST' })).rejects.toMatchObject({
    name: 'QuotaExceededError',
    message: expect.stringContaining('quota exceeded'),
  })
})
```

Adapt the import and the `apiFetch` call shape to what that file already uses.

- [ ] **Step 2: Run it**

Run from `lugo-web-client`: `npm test -- src/api/client.test.ts`
Expected: FAIL — no such error class.

- [ ] **Step 3: Add the React error class**

In `lugo-web-client/src/api/client.ts`:

```ts
// A 429 is not a failure of the app -- it is the app working as configured. It
// gets its own type so a screen can say "you are out of budget" instead of
// "something went wrong".
export class QuotaExceededError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'QuotaExceededError'
  }
}
```

and in `apiFetch`, after the response is received and before the existing error handling, throw it on a 429, reading `detail` from the JSON body and falling back to a plain sentence when the body is unreadable. Read the existing error path first and match its structure — do not bypass the refresh logic.

- [ ] **Step 4: Add the static-UI helper and use it**

In `apps/api_gateway/app/static/js/helpers.js`:

```javascript
// A 429 carries a precise reason from the server ("user quota exceeded for u1:
// $12.04 / $12.00 (monthly)"). Surfacing it verbatim is the difference between a
// user knowing they are out of budget and thinking the app is broken.
export function quotaMessage(resp, body) {
  if (!resp || resp.status !== 429) return "";
  const detail = (body && body.detail) || "";
  return detail || "Quota exceeded - no budget left for this request.";
}
```

In `stt-batch.js` and `tts-batch.js`, wherever a non-ok response is turned into a status message, check `quotaMessage(resp, body)` first and print that when non-empty. Read each file's existing error path and integrate; do not restructure it.

- [ ] **Step 5: Verify**

```bash
cd lugo-web-client && npm test && npm run build && cd ..
node --check apps/api_gateway/app/static/js/helpers.js
node --check apps/api_gateway/app/static/js/stt-batch.js
node --check apps/api_gateway/app/static/js/tts-batch.js
grep -nE '[‘’“”]' apps/api_gateway/app/static/js/helpers.js apps/api_gateway/app/static/js/stt-batch.js apps/api_gateway/app/static/js/tts-batch.js || echo "no smart quotes"
```
Expected: the React suite passes (report the count) and the build succeeds; `node --check` silent; no smart quotes.

- [ ] **Step 6: Commit**

Commit the submodule change INSIDE `lugo-web-client` on its current branch, and the static UI change in the parent. **Do not stage the `lugo-web-client` gitlink in the parent** — another session owns that pending change.

```bash
cd lugo-web-client
git add src/api/client.ts src/api/client.test.ts
git commit -m "feat(api): throw QuotaExceededError on 429"
cd ..
git add apps/api_gateway/app/static/js/helpers.js apps/api_gateway/app/static/js/stt-batch.js apps/api_gateway/app/static/js/tts-batch.js
git commit -m "feat(admin-ui): surface the quota reason on a 429"
```

---

### Task 9: Full-suite gate + docs

**Files:**
- Modify: `docs/superpowers/specs/2026-07-23-provider-management-usage-quota-design.md`

- [ ] **Step 1: Full suite**

Run: `.venv/bin/python -m pytest tests/unit -q`
Expected: all pass. The baseline before this plan was 1366. If something fails, clear stale bytecode (`find apps tests -name __pycache__ -prune -exec rm -rf {} +`) and re-run; if a real failure remains, STOP and report BLOCKED with the output.

- [ ] **Step 2: Verify imports and JS**

```bash
.venv/bin/python -c "import app.main"
for f in pricing quotas usage-me helpers stt-batch tts-batch; do node --check apps/api_gateway/app/static/js/$f.js || echo "FAILED $f"; done
```
Expected: no output.

- [ ] **Step 3: Prove the inventory gate still bites**

The whole point of Phase B is a gate that fails when someone forgets. Re-run the demonstration one final time on the finished branch: add a stray `await provider.synthesize(payload)` line to `apps/api_gateway/app/api/routes/health.py`, run `.venv/bin/python -m pytest tests/unit/test_paid_call_site_inventory.py -q`, confirm it FAILS naming that file, remove the line, and confirm `git diff --exit-code apps/api_gateway/app/api/routes/health.py` is clean. Quote both outputs.

- [ ] **Step 4: Record what shipped**

Append to `docs/superpowers/specs/2026-07-23-provider-management-usage-quota-design.md`:

```markdown
## 15. Metering gaps đã đóng + cơ chế chống bỏ sót (2026-07-26)

Theo plan `plans/2026-07-26-close-metering-gaps.md`:
- **`WS /v1/stt/stream`**: gate lúc connect và mỗi lần `flush`/`end`; đo theo giây
  audio NHẬN được (trước VAD — provider tính tiền theo phần nó xử lý), một row mỗi
  lần finalize, cộng một row cuối cho phần chưa flush khi disconnect.
- **`POST /v1/tts/stream`**: thêm `Request` để có identity (trước đó không có),
  gate đồng bộ trả 429 trước khi spawn job, đo theo từng chunk đã synthesize.
- **Cơ chế chống bỏ sót** (`tests/unit/test_paid_call_site_inventory.py`): liệt kê
  mọi call site gọi provider từ source và bắt buộc mỗi cái phải có status + lý do +
  tên test bao phủ. Thêm call site mới, thêm call thứ hai vào file đã liệt kê, hay
  khai một test không tồn tại → CI đỏ. Kèm
  `tests/unit/test_every_paid_entry_point_meters.py` chạy thật từng entry point và
  assert có row trong DB — cái đầu bắt "quên", cái sau bắt "khai sai".
- **Add-time test call của Model Registry**: ĐO nhưng KHÔNG gate (`request_id =
  "registry-test-call"`) — admin hết quota vẫn phải test được provider để sửa
  đúng cái config làm họ hết quota.
- REST metering ghi `profile_id` khi có `?profile=`; `/chat` resolve trong guard;
  tab Pricing bỏ row sentinel (giá đặt ở đó không bao giờ khớp).
- **Hiển thị**: tab Quotas có cột Spent (%, đỏ khi vượt); `/v1/usage/me` trả thêm
  `limits` (chỉ quota user của chính caller + global, không lộ cross-tenant) và My
  Usage hiện chúng; 429 nay hiện đúng lý do thay vì lỗi chung chung (helper
  `quotaMessage` cho static UI, `QuotaExceededError` cho React client).

Còn lại: rollup `usage_counters` + prune `usage_events` (442 row, có index — chưa
cần); `status="error"` cho call lỗi sau khi provider đã tính tiền; `quota_store`
cache theo process (nhiều worker sẽ stale).
```

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-07-23-provider-management-usage-quota-design.md
git commit -m "docs: record the metering gaps closed and the anti-omission harness"
```

- [ ] **Step 6: Report, do not push**

Report the final test count and the commit list. `main` auto-deploys and this branch is not merged — merging is the user's call.

---

## Self-Review

**1. Coverage of the audit backlog this plan claims:**
- *`POST /v1/tts/stream` and `WS /v1/stt/stream` unmetered and ungated* → Tasks 1, 2 (the only two real money leaks left).
- *"make it impossible to miss"* → Task 3 (inventory: catches a new file, a new method, an extra call in a known file, and a fabricated covering-test name) plus Task 4 (behavioral: a stub cannot satisfy it, the assertion reads the database). Both tasks include a step that **proves the gate can fail** — a gate nobody has watched fail is not a gate.
- *"make it impossible to cheat"* → the Global Constraint requiring every behavioral test to be confirmed RED with its output recorded, plus Task 3's fourth assertion (a classification must name a test file that exists), plus the explicit red-then-restore demonstrations in Tasks 3, 4 and 9.
- *`profile_id=""` in REST; `conversation.py` resolver outside its guard; Pricing tab sentinel rows* → Task 6.
- *No spend/limit display; no 429 handling* → Tasks 7, 8.
- *Model Registry add-time calls* → Task 5, with the meter-don't-gate decision and its rationale stated in the code, the test, and the docs.

**2. Deliberately out of scope, and why:** the `usage_counters` rollup (442 rows with indexes on `ts`/`user_id`/`provider_id` — premature), `usage_events` retention (same), `status="error"` rows for a call that failed after the provider billed (needs a per-provider judgment about when billing actually occurred, which is a spec question, not a coding one), and `quota_store`'s per-process cache (a systemic pattern shared with `ProviderStore` — worth its own change, not a quota-specific patch). All four are recorded in Task 9's doc block so they are not silently dropped.

**3. Placeholder scan:** every step carries literal code or a literal command. Where a step needs judgment the plan names it and bounds it: Task 1 Step 4's insertion into an existing loop (with the invariant — record exactly once on every exit path), Task 4 Step 1's note on the stub's `open_stream`, Task 5 Step 3's check for an existing `Request`/`logger`, Task 6 Step 1's reuse of the file's real stub names, Task 8 Steps 3-4's integration into existing error paths. Task 3 Step 2 predicts its own failure (the covering-test file does not exist until Task 5) so the implementer is not tempted to weaken the assertion.

**4. Type consistency:** `record_usage`, `quota_gate`, `resolve_usage_model`, `current_spend` are all pre-existing and used with their real signatures throughout. `_quota_message(user_id) -> str` and `_record_stream_usage() -> None` are defined and used within Task 1. `quotaMessage(resp, body)` and `QuotaExceededError` are defined and used within Task 8. The `_CLASSIFIED` table in Task 3 names `tests/unit/test_stt_stream_metering.py` (Task 1), `tests/unit/test_tts_stream_metering.py` (Task 2) and `tests/unit/test_model_registry_test_call_metering.py` (Task 5) — all three are created by this plan, and Task 3's own step ordering accounts for the third not existing yet.

**5. Ordering:** Tasks 1 and 2 must precede Task 3 (its table describes the post-fix inventory) and Task 4 (its stream tests would fail otherwise). Task 5 must follow Task 3 (it creates the file Task 3's fourth assertion demands). Tasks 6, 7, 8 are independent of each other and of Phase B. Task 9 last.
