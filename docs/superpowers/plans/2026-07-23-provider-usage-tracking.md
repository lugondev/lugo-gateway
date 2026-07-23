# Provider Usage Attribution + Pricing Implementation Plan (Plan 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Record per-request usage (LLM prompt/completion tokens, STT audio-seconds, TTS characters) attributed to `user_id`/`profile_id`, converted to `cost_usd` from each model's `config.price`, and expose it via a summary API. This is the "token statistics" layer on top of the Provider feature. Quota enforcement is a SEPARATE later plan (Plan 3) — do NOT build blocking here.

**Architecture:** One append-only `usage_events` table (new `Base` model, auto-created by `create_all`). A pure `compute_cost(price, …)` fn. A best-effort async `record_usage(...)` that resolves `provider_id` from the Model Registry, computes cost, and inserts a row — it NEVER raises into the caller (a metering failure must never break a conversation turn). Usage is captured at the **call sites** (per the codebase map: identity + measurements already coexist there; providers are not threaded with identity). The LLM streaming path needs a small change to actually surface `usage`. A read API aggregates the table.

**Tech Stack:** FastAPI, SQLAlchemy async (`db_session`), SQLite, pytest + pytest-asyncio, `fastapi.testclient.TestClient`.

**Spec:** `docs/superpowers/specs/2026-07-23-provider-management-usage-quota-design.md` §4-§6, §8. Builds on the merged Provider backend (`providers` table, `model_registry_store`, `config.provider_id`, `config.price`).

## Global Constraints

- Python 3.12 venv; run tests from repo ROOT: `.venv/bin/python -m pytest tests/unit/<file> -v`. `pyproject.toml`: `asyncio_mode="auto"` (no `@pytest.mark.asyncio` needed but repo commonly adds it — either is fine); `_tmp_db` is `autouse=True` → NEVER a test-function parameter. `TestClient` is synchronous.
- New table only (no ALTER of existing tables). `create_all` in `db/engine.py::init_db` creates `usage_events`.
- **Metering is best-effort & non-blocking:** `record_usage` and every call-site recording MUST be wrapped so an exception (bad price, DB hiccup, missing usage) logs and is swallowed — it must NEVER propagate into an STT/TTS/LLM turn. Never fabricate token counts: if a provider returns no `usage` (e.g. local Ollama), record with `prompt_tokens=None, completion_tokens=None, native_amount=0, cost_usd=0.0`.
- `cost_usd` comes only from `config.price`; a model with no price → `cost_usd = 0.0` (usage still recorded in native units, still attributed).
- Units: LLM `unit="tokens"` (+ prompt/completion split), STT `unit="seconds"`, TTS `unit="chars"`.
- Pricing `config.price` shapes: LLM `{"unit":"1M_tokens","in":<usd/1M in>,"out":<usd/1M out>}`; STT `{"unit":"minute","rate":<usd/min>}`; TTS `{"unit":"1k_chars","rate":<usd/1k chars>}`.
- Do NOT push (main auto-deploys prod). Git identity `lugondev <lugondev@gmail.com>`. Leave submodules/.dockerignore untouched.

---

### Task 1: `UsageEvent` model

**Files:**
- Modify: `apps/api_gateway/app/services/db/models.py` (append after `Provider`)
- Test: `tests/unit/test_usage_event_model.py`

**Interfaces:**
- Produces: `UsageEvent` ORM (table `usage_events`), columns: `id, ts, user_id, profile_id, provider_id, kind, engine, model_id, unit, native_amount, prompt_tokens, completion_tokens, cost_usd, request_id, status`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_usage_event_model.py
from sqlalchemy import select
from app.services.db.engine import db_session, init_db
from app.services.db.models import UsageEvent


async def test_usage_event_roundtrips():
    await init_db()
    async with db_session() as s:
        s.add(UsageEvent(
            id="u1", user_id="user-a", profile_id="p1", provider_id="prov-1",
            kind="llm", engine="openrouter", model_id="qwen-max", unit="tokens",
            native_amount=1500, prompt_tokens=1000, completion_tokens=500,
            cost_usd=0.0021, status="ok",
        ))
        await s.commit()
    async with db_session() as s:
        row = (await s.execute(select(UsageEvent))).scalars().one()
    assert row.kind == "llm" and row.prompt_tokens == 1000
    assert row.cost_usd == 0.0021
    assert row.status == "ok"
    assert row.request_id is None
```

- [ ] **Step 2: Run — expect FAIL** (`cannot import name 'UsageEvent'`)

Run: `.venv/bin/python -m pytest tests/unit/test_usage_event_model.py -v`

- [ ] **Step 3: Add the model** (append after the `Provider` class in `models.py`; `Integer`, `Float`? — check imports: `Float` is NOT imported yet, add it to the `from sqlalchemy import ...` line alongside `Integer`)

```python
class UsageEvent(Base):
    __tablename__ = "usage_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    # "" = shared-device / anonymous bucket (matches memory user-scoping convention).
    user_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    profile_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    # "" when the model isn't linked to a Provider (local engine / own creds).
    provider_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    kind: Mapped[str] = mapped_column(String(8), index=True)      # stt|tts|llm
    engine: Mapped[str] = mapped_column(String(64))
    model_id: Mapped[str] = mapped_column(String(128))
    unit: Mapped[str] = mapped_column(String(16))                 # tokens|seconds|chars
    native_amount: Mapped[float] = mapped_column(Float, default=0.0)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok|error
```

- [ ] **Step 4: Run — expect PASS.** Run: `.venv/bin/python -m pytest tests/unit/test_usage_event_model.py -v`

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/db/models.py tests/unit/test_usage_event_model.py
git -c user.name=lugondev -c user.email=lugondev@gmail.com commit -m "feat(usage): add UsageEvent model"
```

---

### Task 2: `compute_cost` pricing function

**Files:**
- Create: `apps/api_gateway/app/services/usage/__init__.py` (empty)
- Create: `apps/api_gateway/app/services/usage/pricing.py`
- Test: `tests/unit/test_usage_pricing.py`

**Interfaces:**
- Produces: `compute_cost(price: dict | None, prompt_tokens: int | None, completion_tokens: int | None, native_amount: float) -> float`. Switches on `price["unit"]`. Returns `0.0` for `price=None`, empty, or an unrecognized unit.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_usage_pricing.py
from app.services.usage.pricing import compute_cost


def test_llm_1m_tokens_split():
    price = {"unit": "1M_tokens", "in": 0.15, "out": 0.60}
    # 1000 in, 500 out
    cost = compute_cost(price, 1000, 500, 1500)
    assert abs(cost - (1000/1_000_000*0.15 + 500/1_000_000*0.60)) < 1e-12


def test_stt_minute():
    price = {"unit": "minute", "rate": 0.006}
    assert abs(compute_cost(price, None, None, 90.0) - (90.0/60*0.006)) < 1e-12


def test_tts_1k_chars():
    price = {"unit": "1k_chars", "rate": 0.015}
    assert abs(compute_cost(price, None, None, 500.0) - (500.0/1000*0.015)) < 1e-12


def test_missing_or_unknown_price_is_zero():
    assert compute_cost(None, 1000, 500, 1500) == 0.0
    assert compute_cost({}, 1000, 500, 1500) == 0.0
    assert compute_cost({"unit": "furlongs"}, None, None, 5) == 0.0
```

- [ ] **Step 2: Run — expect FAIL** (module missing). Run: `.venv/bin/python -m pytest tests/unit/test_usage_pricing.py -v`

- [ ] **Step 3: Implement**

```python
# apps/api_gateway/app/services/usage/pricing.py
"""Convert a usage measurement to USD using a model's config.price.

Price shapes (stored in a Model Registry entry's config["price"]):
  LLM: {"unit": "1M_tokens", "in": <usd per 1M input>, "out": <usd per 1M output>}
  STT: {"unit": "minute",    "rate": <usd per minute of audio>}
  TTS: {"unit": "1k_chars",  "rate": <usd per 1000 characters>}
Anything missing/unrecognized costs 0.0 (usage is still recorded, just uncosted).
"""
from __future__ import annotations


def compute_cost(price, prompt_tokens, completion_tokens, native_amount):
    if not price or not isinstance(price, dict):
        return 0.0
    unit = price.get("unit")
    if unit == "1M_tokens":
        pin = float(price.get("in", 0.0))
        pout = float(price.get("out", 0.0))
        return (prompt_tokens or 0) / 1_000_000 * pin + (completion_tokens or 0) / 1_000_000 * pout
    if unit == "minute":
        return float(native_amount or 0.0) / 60.0 * float(price.get("rate", 0.0))
    if unit == "1k_chars":
        return float(native_amount or 0.0) / 1000.0 * float(price.get("rate", 0.0))
    return 0.0
```

- [ ] **Step 4: Run — expect PASS.**  Run: `.venv/bin/python -m pytest tests/unit/test_usage_pricing.py -v`

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/usage/ tests/unit/test_usage_pricing.py
git -c user.name=lugondev -c user.email=lugondev@gmail.com commit -m "feat(usage): compute_cost pricing function"
```

---

### Task 3: `record_usage` recorder (best-effort, resolves provider_id + cost)

**Files:**
- Create: `apps/api_gateway/app/services/usage/recorder.py`
- Test: `tests/unit/test_usage_recorder.py`

**Interfaces:**
- Consumes: `compute_cost` (T2), `model_registry_store` (for provider_id + price lookup), `db_session`, `UsageEvent`.
- Produces:
  `async def record_usage(*, user_id: str, profile_id: str, kind: str, engine: str, model_id: str, unit: str, native_amount: float, prompt_tokens: int | None = None, completion_tokens: int | None = None, request_id: str | None = None, status: str = "ok") -> None`
  Behavior: look up the registry entry `find(kind, engine, model_id)`; `provider_id = entry.config.get("provider_id","")`, `price = entry.config.get("price")`; `cost = compute_cost(price, prompt_tokens, completion_tokens, native_amount)`; insert a `UsageEvent`. **Wrap the whole body in try/except**, logging on failure and returning None — must never raise. `user_id`/`profile_id` None → coerce to `""`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_usage_recorder.py
from sqlalchemy import select
from app.services.db.engine import db_session, init_db
from app.services.db.models import UsageEvent
from app.services.model_registry.store import model_registry_store
from app.services.usage.recorder import record_usage


async def test_records_with_cost_and_provider_from_registry():
    await init_db()
    await model_registry_store.create(
        "llm", "openrouter", "qwen-max", "Qwen Max",
        config={"provider_id": "prov-9", "price": {"unit": "1M_tokens", "in": 0.15, "out": 0.60}},
        is_default=True,
    )
    await record_usage(user_id="u1", profile_id="p1", kind="llm", engine="openrouter",
                       model_id="qwen-max", unit="tokens", native_amount=1500,
                       prompt_tokens=1000, completion_tokens=500)
    async with db_session() as s:
        row = (await s.execute(select(UsageEvent))).scalars().one()
    assert row.provider_id == "prov-9"
    assert row.prompt_tokens == 1000 and row.completion_tokens == 500
    assert abs(row.cost_usd - (1000/1e6*0.15 + 500/1e6*0.60)) < 1e-12
    assert row.user_id == "u1"


async def test_no_registry_entry_records_zero_cost_no_provider():
    await init_db()
    await record_usage(user_id="", profile_id="", kind="tts", engine="vieneu",
                       model_id="v1", unit="chars", native_amount=200)
    async with db_session() as s:
        row = (await s.execute(select(UsageEvent))).scalars().one()
    assert row.provider_id == "" and row.cost_usd == 0.0 and row.native_amount == 200


async def test_never_raises_on_bad_input(monkeypatch):
    # Force the store lookup to blow up; record_usage must swallow it.
    async def boom(*a, **k): raise RuntimeError("db down")
    monkeypatch.setattr(model_registry_store, "find", boom)
    await init_db()
    # Must NOT raise:
    await record_usage(user_id="u", profile_id="p", kind="llm", engine="x",
                       model_id="y", unit="tokens", native_amount=1)
```

- [ ] **Step 2: Run — expect FAIL** (module missing). Run: `.venv/bin/python -m pytest tests/unit/test_usage_recorder.py -v`

- [ ] **Step 3: Implement**

```python
# apps/api_gateway/app/services/usage/recorder.py
from __future__ import annotations

import logging
import uuid

from app.services.db.engine import db_session
from app.services.db.models import UsageEvent
from app.services.model_registry.store import model_registry_store
from app.services.usage.pricing import compute_cost

logger = logging.getLogger(__name__)


async def record_usage(*, user_id, profile_id, kind, engine, model_id, unit,
                       native_amount, prompt_tokens=None, completion_tokens=None,
                       request_id=None, status="ok"):
    """Best-effort append of one usage row. Resolves provider_id + price from the
    Model Registry entry, computes cost. NEVER raises into the caller — a metering
    failure must not break the STT/TTS/LLM turn that triggered it."""
    try:
        entry = await model_registry_store.find(kind, engine, model_id)
        cfg = (entry or {}).get("config") or {}
        provider_id = cfg.get("provider_id") or ""
        cost = compute_cost(cfg.get("price"), prompt_tokens, completion_tokens, native_amount)
        async with db_session() as s:
            s.add(UsageEvent(
                id=str(uuid.uuid4()), user_id=user_id or "", profile_id=profile_id or "",
                provider_id=provider_id, kind=kind, engine=engine, model_id=model_id,
                unit=unit, native_amount=float(native_amount or 0.0),
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                cost_usd=cost, request_id=request_id, status=status,
            ))
            await s.commit()
    except Exception as exc:  # noqa: BLE001 - metering must never break a turn
        logger.warning("record_usage failed (%s/%s/%s): %s", kind, engine, model_id, exc)
```

- [ ] **Step 4: Run — expect PASS.** Run: `.venv/bin/python -m pytest tests/unit/test_usage_recorder.py -v`

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/usage/recorder.py tests/unit/test_usage_recorder.py
git -c user.name=lugondev -c user.email=lugondev@gmail.com commit -m "feat(usage): best-effort record_usage recorder"
```

---

### Task 4: Surface LLM `usage` from the responder (stream + non-stream)

**Files:**
- Modify: `apps/api_gateway/app/services/conversation/responder.py` (`OpenAICompatResponder`: `__init__`, `reply`, `_stream_history`; `_tool_then_stream` optional)
- Test: `tests/unit/test_responder_usage.py`

**Interfaces:**
- Produces: `OpenAICompatResponder.last_usage: dict | None` — set to `{"prompt_tokens": int, "completion_tokens": int}` after a `reply()` or a streamed turn completes, or `None` if the endpoint returned no usage. Initialized to `None` in `__init__`; reset to `None` at the start of each `reply`/`_stream_history` call.
- Behavior: `_stream_history` must add `"stream_options": {"include_usage": True}` to the request JSON and, while iterating SSE lines, capture the `usage` object from any chunk that carries one (the final pre-`[DONE]` chunk has `choices: []` and a `usage`). `reply` reads `data.get("usage")`. Guard everything: a missing/partial usage leaves `last_usage = None`.

- [ ] **Step 1: Read the current code** — `sed -n '182,324p' apps/api_gateway/app/services/conversation/responder.py`. Confirm the `__init__` field list and the `_stream_history` SSE loop shape (the `data == "[DONE]"` break and the `json.loads(data)["choices"][0]...` line) before editing.

- [ ] **Step 2: Write the failing test**

```python
# tests/unit/test_responder_usage.py
import json
import pytest
from app.services.conversation.responder import OpenAICompatResponder


class _FakeStreamResp:
    def __init__(self, lines): self._lines = lines; self.status_code = 200
    def raise_for_status(self): pass
    async def aiter_lines(self):
        for ln in self._lines: yield ln
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


class _FakeClient:
    def __init__(self, lines): self._lines = lines
    def stream(self, *a, **k): return _FakeStreamResp(self._lines)
    async def aclose(self): pass


async def test_stream_captures_usage_chunk():
    lines = [
        'data: ' + json.dumps({"choices": [{"delta": {"content": "Hello."}}]}),
        'data: ' + json.dumps({"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 3}}),
        'data: [DONE]',
    ]
    r = OpenAICompatResponder(base_url="http://x/v1", api_key="", model="m", system_prompt="s")
    r._client = _FakeClient(lines)
    out = [chunk async for chunk in r._stream_history([{"role": "user", "content": "hi"}])]
    assert "".join(out).strip() == "Hello."
    assert r.last_usage == {"prompt_tokens": 12, "completion_tokens": 3}


async def test_stream_without_usage_leaves_none():
    lines = [
        'data: ' + json.dumps({"choices": [{"delta": {"content": "Hi."}}]}),
        'data: [DONE]',
    ]
    r = OpenAICompatResponder(base_url="http://x/v1", api_key="", model="m", system_prompt="s")
    r._client = _FakeClient(lines)
    _ = [c async for c in r._stream_history([{"role": "user", "content": "hi"}])]
    assert r.last_usage is None
```

- [ ] **Step 3: Run — expect FAIL** (`last_usage` missing / not captured). Run: `.venv/bin/python -m pytest tests/unit/test_responder_usage.py -v`

- [ ] **Step 4: Implement** (in `responder.py`)
  - In `__init__`, add `self.last_usage = None`.
  - In `_stream_history`: set `self.last_usage = None` at entry; add `"stream_options": {"include_usage": True}` to the request `json={...}`; inside the SSE loop, BEFORE the `choices[0]` delta access (which assumes a non-empty `choices`), parse the chunk and, if it contains a `usage` dict, store `self.last_usage = {"prompt_tokens": u.get("prompt_tokens"), "completion_tokens": u.get("completion_tokens")}` and `continue` (a usage chunk has `choices: []`, so the existing `["choices"][0]` access MUST be guarded — use `.get("choices") or []` and skip when empty). Keep yielding sentences exactly as before.
  - In `reply`: after `data = resp.json()`, set `self.last_usage` from `data.get("usage")` (guarded) before returning.
  - (Optional) `_tool_then_stream` hands off to `_stream_history`, which sets `last_usage` from the final streamed answer — no change needed.

- [ ] **Step 5: Run — expect PASS.** Run: `.venv/bin/python -m pytest tests/unit/test_responder_usage.py -v`

- [ ] **Step 6: Regression** — `.venv/bin/python -m pytest tests/unit/test_responder_llm_registry.py tests/unit/test_responder_provider_creds.py -v` (streaming/tool tests must still pass).

- [ ] **Step 7: Commit**

```bash
git add apps/api_gateway/app/services/conversation/responder.py tests/unit/test_responder_usage.py
git -c user.name=lugondev -c user.email=lugondev@gmail.com commit -m "feat(usage): surface LLM token usage from responder (stream + non-stream)"
```

---

### Task 5: Meter the conversation core (ConversationSession) — LLM/STT/TTS + fix Lugo identity

**Files:**
- Modify: `apps/api_gateway/app/services/conversation/session.py`
- Modify: `apps/api_gateway/app/api/routes/lugo.py` (pass `identity_user_id` into `SessionRuntimeConfig`)
- Test: `tests/unit/test_session_usage_metering.py`

**Interfaces:**
- Consumes: `record_usage` (T3), `OpenAICompatResponder.last_usage` (T4).
- Behavior: within `ConversationSession`, after each turn's STT / LLM / TTS work completes, call `await record_usage(...)` (best-effort — the recorder already swallows errors, but ALSO don't let building the args raise). Identity: `user_id = self.cfg.identity_user_id or ""`, `profile_id = self.cfg.profile_name or ""`.
  - **STT** (call site ~session.py:555; audio seconds available via the WAV built at ~:532 using `wav_duration_seconds`, or the `speech_ms` already in scope → seconds): `record_usage(kind="stt", engine=<the stt engine used: turn_engine or cfg.stt_engine>, model_id=<turn_model or self.stt_model_id>, unit="seconds", native_amount=<seconds>)`.
  - **LLM** (call sites ~session.py:511 and :570, `reply_stream`): after the stream is fully consumed, read the responder's `last_usage`; `record_usage(kind="llm", engine=<profile.llm.engine>, model_id=<profile.llm.model>, unit="tokens", native_amount=(pt+ct if both else 0), prompt_tokens=pt, completion_tokens=ct)` where `pt/ct` come from `last_usage` (may be None → pass None, native_amount 0).
  - **TTS** (call sites ~session.py:436/:441; text = the `sentence`/`payload.text`): `record_usage(kind="tts", engine=cfg.tts_engine, model_id=cfg.tts_model, unit="chars", native_amount=len(text))`.
  - Fix `lugo.py` `SessionRuntimeConfig(...)` (~:117-124) to pass `identity_user_id=identity.user_id` so device turns attribute correctly (mirror the conversation-WS path at conversation.py:279).

- [ ] **Step 1: Read the turn code** — `sed -n '360,580p' apps/api_gateway/app/services/conversation/session.py` and `sed -n '110,210p' apps/api_gateway/app/services/conversation/session.py`. Identify: the responder object variable used at :511/:570 (to read `.last_usage`), how `sentence`/text is named at the TTS calls, how the STT result + seconds are obtained, and the exact `profile.llm.engine`/`.model`, `cfg.stt_engine`/`self.stt_model_id` (+ fast-path `turn_engine`/`turn_model`), `cfg.tts_engine`/`cfg.tts_model` accessors. Do NOT guess — anchor each `record_usage` call on the real variables in scope.

- [ ] **Step 2: Write the failing test** — a focused test that drives ONE turn path and asserts a `UsageEvent` row is written. Because `_run_turn` is heavy, test the smallest real unit you can: if there's a seam to invoke the LLM-record path with a fake responder exposing `last_usage`, use it; otherwise write a test that constructs a minimal `ConversationSession` with stubbed STT/TTS/responder and asserts rows appear in `usage_events` for the kinds exercised. If a full-session harness is impractical, at minimum assert (via monkeypatching `record_usage` with a spy) that the turn calls it once per kind with the right `(kind, engine, model_id, user_id, profile_id)` — a spy test is acceptable here given the integration nature; state that choice in the report.

```python
# tests/unit/test_session_usage_metering.py — shape (adapt to the real seam found in Step 1)
# Prefer asserting real rows in usage_events; fall back to a record_usage spy if
# a full ConversationSession turn can't be driven without audio/LLM I/O.
```

- [ ] **Step 3: Run — expect FAIL.** Run: `.venv/bin/python -m pytest tests/unit/test_session_usage_metering.py -v`

- [ ] **Step 4: Implement** the three best-effort `record_usage` calls at the identified call sites + the `lugo.py` identity fix. Each recording call must sit AFTER the measurement is available and be structured so it cannot break the turn (the recorder swallows its own errors; also avoid `len(None)` etc. by guarding the arg-building). Import `record_usage` inside the function (lazy) if module-level import risks a cycle — check.

- [ ] **Step 5: Run — expect PASS**, then regression on the conversation/session suites: `.venv/bin/python -m pytest tests/unit -q -k "session or conversation or lugo"`.

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/services/conversation/session.py apps/api_gateway/app/api/routes/lugo.py tests/unit/test_session_usage_metering.py
git -c user.name=lugondev -c user.email=lugondev@gmail.com commit -m "feat(usage): meter LLM/STT/TTS in conversation core + fix Lugo identity attribution"
```

---

### Task 6: Meter the REST routes (/chat, /transcribe, /synthesize, livehost)

**Files:**
- Modify: `apps/api_gateway/app/api/routes/conversation.py` (`/chat`), `apps/api_gateway/app/api/routes/stt.py` (`/transcribe`), `apps/api_gateway/app/api/routes/tts.py` (`/synthesize`), and `apps/api_gateway/app/api/routes/livehost.py` (LLM/STT/TTS call sites at :338/:364/:325/:269 if straightforward)
- Test: `tests/unit/test_routes_usage_metering.py`

**Interfaces:**
- Consumes: `record_usage`, `current_user_id(request)` (core/actor).
- Behavior: at each REST call site, after the work completes, best-effort `record_usage(...)` with `user_id=current_user_id(request) or ""`, `profile_id=<the profile query param or "">`. `/transcribe` already computes `wav_duration_seconds` (stt.py:96) → use it as STT seconds. `/synthesize` → `len(payload.text)` chars. `/chat` non-tool path (`reply`) → responder `last_usage`. Keep each recording non-blocking.

- [ ] **Step 1: Read the routes** — `sed -n '95,175p' apps/api_gateway/app/api/routes/conversation.py`, `sed -n '80,105p' apps/api_gateway/app/api/routes/stt.py`, `sed -n '40,90p' apps/api_gateway/app/api/routes/tts.py`. Identify the engine/model in scope at each (for `/transcribe`/`/synthesize` the request or resolved config carries engine/model; for `/chat` use the resolved LLM engine/model).

- [ ] **Step 2: Write the failing test** — drive `/transcribe` and `/synthesize` via `TestClient` (they don't need real models if the provider is stubbed the way existing route tests do — mirror `tests/unit/test_*routes*.py` patterns) and assert a `UsageEvent` row appears with the right kind/user. If a route needs heavy provider setup, use the same stub approach the existing route tests use; otherwise fall back to a `record_usage` spy and assert the call args (state the choice in the report).

- [ ] **Step 3: Run — expect FAIL.**

- [ ] **Step 4: Implement** the best-effort recording at each route call site.

- [ ] **Step 5: Run — expect PASS**, regression: `.venv/bin/python -m pytest tests/unit -q -k "stt or tts or conversation or livehost"`.

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/api/routes/conversation.py apps/api_gateway/app/api/routes/stt.py apps/api_gateway/app/api/routes/tts.py apps/api_gateway/app/api/routes/livehost.py tests/unit/test_routes_usage_metering.py
git -c user.name=lugondev -c user.email=lugondev@gmail.com commit -m "feat(usage): meter STT/TTS/LLM REST routes"
```

---

### Task 7: Usage summary API (`/v1/usage/summary` admin + `/v1/usage/me`)

**Files:**
- Create: `apps/api_gateway/app/services/usage/query.py` (aggregation helpers)
- Create: `apps/api_gateway/app/api/routes/usage.py`
- Modify: `apps/api_gateway/app/main.py` (register router)
- Modify: `apps/api_gateway/app/core/auth_guard.py` (`/v1/usage` admin; carve out `/v1/usage/me` for any logged-in user)
- Test: `tests/unit/test_usage_query.py`, `tests/unit/test_usage_routes.py`

**Interfaces:**
- Produces:
  - `query.py`: `async def summarize(group_by: str, period_key: str | None = None) -> list[dict]` — SUM(cost_usd), SUM(native_amount), COUNT(*) grouped by one of `user|provider|model|kind|engine`; optional `period_key` "YYYY-MM" filters `ts` to that month. And `async def summarize_for_user(user_id: str, period_key=None) -> list[dict]` grouped by kind/model.
  - `routes/usage.py`: `GET /v1/usage/summary?group_by=&period=` (admin) → `{success, data}`; `GET /v1/usage/me?period=` → the caller's own totals (uses `current_user_id`).
  - auth_guard: `/v1/usage` in `_ADMIN_PREFIXES`; `/v1/usage/me` added to the any-logged-in list (mirror how `/v1/model_registry/options` is carved out of the admin `/v1/model_registry` prefix).

- [ ] **Step 1: Write failing tests** (`test_usage_query.py`: insert a few `UsageEvent` rows, assert `summarize("provider")` / `summarize("kind")` sums + `summarize_for_user`; `test_usage_routes.py`: admin sees `/summary`, non-admin gets 403 on `/summary` but 200 on `/me`, mirror the auth pattern from `tests/unit/test_model_registry_routes.py`).

(Write concrete rows + assertions — sum of two llm rows' cost equals expected; a non-admin `/summary` → 403; `/me` returns only the caller's rows.)

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** `query.py` (SQLAlchemy `func.sum`/`func.count` + `group_by`), `routes/usage.py`, register in `main.py`, add auth_guard prefixes.

- [ ] **Step 4: Run — expect PASS**, then full suite: `.venv/bin/python -m pytest tests/unit -q`.

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/usage/query.py apps/api_gateway/app/api/routes/usage.py apps/api_gateway/app/main.py apps/api_gateway/app/core/auth_guard.py tests/unit/test_usage_query.py tests/unit/test_usage_routes.py
git -c user.name=lugondev -c user.email=lugondev@gmail.com commit -m "feat(usage): usage summary API (/v1/usage/summary admin + /v1/usage/me)"
```

---

### Task 8: Full-suite gate (controller-run)

- [ ] **Step 1:** `.venv/bin/python -m pytest tests/unit -q` — all pass, no regressions.
- [ ] **Step 2:** `.venv/bin/python -c "import app.main"` — imports clean (routes/models wired).

---

## Deferred (Plan 3 / later)
- Quota enforcement (`quotas` + `usage_counters` rollup + `quota_gate` pre-flight blocking) — SEPARATE plan.
- Admin usage dashboard UI (charts/tables) — can follow once the summary API is proven.
- `usage_counters` rollup table for fast period sums (only needed once quota/real-time dashboards demand it; the summary API queries `usage_events` directly for now).

## Self-Review
- **Spec coverage:** usage_events (T1) ✓; pricing/cost from config.price (T2) ✓; best-effort recorder resolving provider_id (T3) ✓; LLM token capture incl. the stream_options gap (T4) ✓; attribution via identity-at-call-site for all 3 kinds + Lugo identity fix (T5) + REST (T6) ✓; summary + per-user API with admin gate (T7) ✓. Quota deferred per spec (Plan 3).
- **Placeholder scan:** T1-T4, T7 carry complete code. T5/T6 are integration tasks in heavy existing files — they carry explicit line refs, the exact `record_usage` contract, and a "read the surrounding code first, anchor on real variables" instruction plus a stated fallback for the test (spy vs real-row), mirroring how a careful dev instruments existing call sites; not blank placeholders.
- **Consistency:** `record_usage(**kwargs)` signature identical across T3 definition and T5/T6 call sites; `last_usage` shape `{prompt_tokens, completion_tokens}` defined in T4 and consumed in T5/T6; `compute_cost` arg order identical T2↔T3.
- **Risk note:** metering is best-effort/non-blocking by constraint — the recorder swallows errors and callers guard arg-building, so instrumenting the real-time turn path cannot break a conversation.
