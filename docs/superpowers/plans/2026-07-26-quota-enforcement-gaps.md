# Quota Enforcement Gaps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make quota enforcement actually cover what it claims — provider-scoped quotas that currently never fire, two ungated conversation paths, quota rows that can be saved in states the gate silently ignores, and blocks that leave no audit trail.

**Architecture:** Four independent defects in one subsystem. The provider fix routes every gate's `provider_id` lookup through the existing `resolve_usage_model` resolver, the same way `/chat` already does. The audit trail is written inside `quota_gate` itself (one place, six call sites) as a `status="blocked"` row with `cost_usd = 0`, and usage summaries are narrowed to `status="ok"` so refused requests never inflate usage numbers. Validation moves to the `/v1/quotas` routes, where a normalized-and-checked payload is the only thing that can reach the store.

**Tech Stack:** Python 3.12 (FastAPI, SQLAlchemy async, pytest), vanilla ES-module JS for the admin static UI.

## Global Constraints

- **Python:** always `.venv/bin/python` (the venv is 3.12; the system Python lacks the ML wheels).
- **Test scope:** run `tests/unit` of this repo only (`.venv/bin/python -m pytest tests/unit -q`). Never run submodule suites.
- **Never push.** `main` auto-deploys to production. Commit locally only.
- **Branch:** do all work on `feat/quota-enforcement-gaps` (create it off `main` before Task 1). Other sessions share this working tree — do not switch branches mid-task.
- **Pre-existing dirt:** the working tree has unrelated modified files (two under `docs/superpowers/` dated 2026-07-25, and five submodule gitlinks). Never stage, commit, or revert them. Stage files by path; never `git add -A`.
- **ASCII quotes only** in any `.js` / `.html` you touch. A previous session corrupted static UI files with smart quotes (`’ “ ”`), and `node --check` does NOT catch them inside string literals. Verify with a grep and by reading the file back.
- **The gate stays fail-open.** `quota_gate` may only ever raise `QuotaExceededError`. Every other error — including anything the new audit-row write does — must log and allow. A bug in quota bookkeeping must never deny service.
- **Metering must never raise into a caller.** New `record_usage` calls are wrapped in `try/except Exception` + `logger.warning`, exactly like the existing ones.
- **A blocked request is not usage.** Audit rows carry `cost_usd = 0` and `native_amount = 0` so they can never feed back into the spend they were caused by.

---

## Reference: the four defects, as measured

Read this before Task 1. Each was verified by running code against the real registry shape, not by reading.

**1. Provider-scoped quotas almost never fire.** Every gate resolves `provider_id` by looking up `model_registry_store.find(kind, engine, model_id)`, but passes a `model_id` that is usually blank, so the lookup misses and `provider_id` stays `""` — and `_applies()` in `quota/gate.py` skips every provider-scoped quota when `provider_id` is falsy. Measured against a registry shaped like production:

```
/transcribe   find(stt, qwencloud, '')  -> None  -> provider quota NOT enforced
/synthesize   find(tts, vieneu, '')     -> None  -> provider quota NOT enforced
turn (no pin) find(llm, OA, '')         -> None  -> provider quota NOT enforced
/chat                                    -> prov-oa   (already fixed: it calls resolve_usage_model first)
```

User-scope and global-scope quotas are unaffected — they never look at `provider_id`.

**2. `routes/livehost.py` has no gate at all.** `grep -n quota apps/api_gateway/app/api/routes/livehost.py` returns nothing. Its two turn entry points (`_run_voice_turn` at ~line 350 and `_run_social_turn` at ~line 393) run STT + LLM + TTS with no quota check, so an over-limit user simply uses that endpoint.

**3. Quota rows can be saved in states the gate ignores.** Verified through the real HTTP API:

```
scope=user with scope_id=""  -> 200   (then silently applies to the shared-device bucket, user_id "")
limit_usd = -5               -> 200   (gate's `limit_usd > 0` guard means it never fires)
limit_usd = 0                -> 200   (same -- reads as "unlimited", the opposite of what an admin means)
duplicate (user, u1, monthly)-> 200   (two enabled rows for one scope)
```

**4. No `status="blocked"` audit row.** `grep -rn '"blocked"' apps/api_gateway/app` returns nothing, though the design spec (§7 of `docs/superpowers/specs/2026-07-23-provider-management-usage-quota-design.md`) requires one. A refused request leaves no trace anywhere.

### Interfaces you will be working against (already verified — do not re-derive)

- `apps/api_gateway/app/services/quota/gate.py`: `async def quota_gate(*, user_id: str, provider_id: str) -> None`; `class QuotaExceededError(Exception)` with attributes `scope, scope_id, limit_usd, spend_usd, period` and a ready-made `str()` like `user quota exceeded for u1: $12.0400 / $12.0000 (monthly)`; `async def current_spend(*, scope, scope_id, period) -> float`; `_applies(q, user_id, provider_id)`.
- `apps/api_gateway/app/services/usage/attribution.py`: `async def resolve_usage_model(kind: str, engine: str, model_id: str) -> tuple[str, str]` — fills blanks where provable, never raises, never guesses. This is what `/chat` already uses for its gate lookup.
- `apps/api_gateway/app/services/usage/recorder.py`: `async def record_usage(*, user_id, profile_id, kind, engine, model_id, unit, native_amount, prompt_tokens=None, completion_tokens=None, request_id=None, status="ok") -> None` — swallows all its own errors, resolves attribution internally, computes `cost_usd` from the price (so `native_amount=0` yields `0.0`).
- `apps/api_gateway/app/services/db/models.py:136` `UsageEvent`: `status` is `String(16)` defaulting to `"ok"`; `kind` is `String(8)` and NOT nullable, so a row cannot be written without one.
- `apps/api_gateway/app/services/quota/store.py`: `quota_store` with async `list_all()`, `list_enabled()`, `get(id)`, `create(scope, scope_id, limit_usd, period, enabled)`, `set_fields(id, **fields)`, `delete(id)`, and a sync `invalidate()`.
- Test style: `tests/unit/test_quota_gate.py` and `tests/unit/test_memory_quota_gate.py` are the closest references. Newer usage tests are marker-free async with `await init_db()`; the memory ones use `@pytest.mark.asyncio`. Match the file you are editing; for new files use the marker-free style.
- To make a quota trip in a test: create a registry row with a price, `record_usage` enough `native_amount` to exceed it, then `quota_store.create(...)`. Call `quota_store.invalidate()` first — the store caches in memory across tests.

---

### Task 1: Write a `status="blocked"` audit row when the gate blocks

**Files:**
- Modify: `apps/api_gateway/app/services/quota/gate.py`
- Modify: `apps/api_gateway/app/services/db/models.py` (the `status` column comment, ~line 155)
- Test: `tests/unit/test_quota_blocked_audit.py`

**Interfaces:**
- Consumes: `record_usage` (recorder), `QuotaExceededError` (same module).
- Produces: `quota_gate(*, user_id: str, provider_id: str, kind: str = "", engine: str = "", model_id: str = "", profile_id: str = "") -> None`. The four new parameters are keyword-only with defaults, so every existing caller keeps working. When the gate blocks AND `kind` is non-empty, it writes one `UsageEvent` with `status="blocked"`, `native_amount=0`, `cost_usd=0` before raising.

**Why in the gate rather than at each call site:** there are six call sites and each would need the same six lines. More importantly, an audit trail that depends on every caller remembering to write it is an audit trail with holes.

**Why `kind` gates the write:** `UsageEvent.kind` is a non-nullable `String(8)`. A caller that cannot say what kind of work was refused has nothing meaningful to record, and inventing a placeholder kind would pollute the dashboards' kind breakdown.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_quota_blocked_audit.py`:

```python
"""The design spec (§7) requires a blocked request to leave an audit row.
Without it, a quota block is invisible after the fact: the request never
appears in usage (it did no work) and nothing else records that it happened."""

from sqlalchemy import select

from app.services.db.engine import db_session, init_db
from app.services.db.models import UsageEvent
from app.services.model_registry.store import model_registry_store
from app.services.quota.gate import QuotaExceededError, current_spend, quota_gate
from app.services.quota.store import quota_store
from app.services.usage.recorder import record_usage


async def _rows(status=None):
    async with db_session() as s:
        rows = (await s.execute(select(UsageEvent))).scalars().all()
    return [r for r in rows if status is None or r.status == status]


async def _spend_over_a_one_dollar_user_quota(user_id: str) -> None:
    """Put `user_id` over a $1 monthly quota with one priced usage row."""
    await init_db()
    quota_store.invalidate()
    await model_registry_store.create(
        "llm", "OA", "priced-model", "Priced",
        config={"provider_id": "prov-oa", "price": {"unit": "1M_tokens", "in": 10.0, "out": 0.0}},
    )
    await record_usage(user_id=user_id, profile_id="p", kind="llm", engine="OA",
                       model_id="priced-model", unit="tokens", native_amount=1_000_000,
                       prompt_tokens=1_000_000)
    await quota_store.create(scope="user", scope_id=user_id, limit_usd=1.0, period="monthly")


async def test_blocking_writes_one_audit_row():
    await _spend_over_a_one_dollar_user_quota("u-audit")
    try:
        await quota_gate(user_id="u-audit", provider_id="", kind="stt",
                         engine="qwencloud", model_id="fun-asr", profile_id="pro")
        raise AssertionError("expected the gate to block")
    except QuotaExceededError:
        pass

    blocked = await _rows("blocked")
    assert len(blocked) == 1
    row = blocked[0]
    assert row.kind == "stt" and row.engine == "qwencloud" and row.model_id == "fun-asr"
    assert row.user_id == "u-audit" and row.profile_id == "pro"
    assert row.unit == "seconds"      # the audit row still says what was refused
    assert row.native_amount == 0.0   # nothing was served
    assert row.cost_usd == 0.0


async def test_a_blocked_row_can_never_feed_the_spend_that_caused_it():
    """The dangerous failure: if a blocked row carried cost, each block would
    raise the spend that triggers the next block."""
    await _spend_over_a_one_dollar_user_quota("u-feedback")
    before = await current_spend(scope="user", scope_id="u-feedback", period="monthly")
    for _ in range(3):
        try:
            await quota_gate(user_id="u-feedback", provider_id="", kind="llm",
                             engine="OA", model_id="priced-model")
        except QuotaExceededError:
            pass
    after = await current_spend(scope="user", scope_id="u-feedback", period="monthly")
    assert after == before
    assert len(await _rows("blocked")) == 3


async def test_no_audit_row_when_the_caller_names_no_kind():
    await _spend_over_a_one_dollar_user_quota("u-nokind")
    try:
        await quota_gate(user_id="u-nokind", provider_id="")
    except QuotaExceededError:
        pass
    assert await _rows("blocked") == []


async def test_allowed_requests_write_no_audit_row():
    await init_db()
    quota_store.invalidate()
    await quota_store.create(scope="user", scope_id="u-under", limit_usd=100.0, period="monthly")
    await quota_gate(user_id="u-under", provider_id="", kind="llm", engine="OA", model_id="m")
    assert await _rows("blocked") == []


async def test_a_failing_audit_write_still_blocks(monkeypatch):
    """The block is the point; the audit row is bookkeeping. A recorder failure
    must not turn a refusal into a served request."""
    await _spend_over_a_one_dollar_user_quota("u-recfail")

    async def boom(**kwargs):
        raise RuntimeError("recorder down")

    monkeypatch.setattr("app.services.quota.gate.record_usage", boom)
    try:
        await quota_gate(user_id="u-recfail", provider_id="", kind="llm",
                         engine="OA", model_id="priced-model")
        raise AssertionError("expected the gate to block even though the audit write failed")
    except QuotaExceededError:
        pass


async def test_the_block_is_logged_with_the_quota_that_tripped(caplog):
    import logging

    await _spend_over_a_one_dollar_user_quota("u-log")
    with caplog.at_level(logging.WARNING, logger="app.services.quota.gate"):
        try:
            await quota_gate(user_id="u-log", provider_id="", kind="llm",
                             engine="OA", model_id="priced-model")
        except QuotaExceededError:
            pass
    blocked_logs = [r for r in caplog.records if "quota exceeded" in r.getMessage()]
    assert blocked_logs, "a block must be visible in the logs, not only in the DB"
    assert "u-log" in blocked_logs[0].getMessage()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_quota_blocked_audit.py -q`
Expected: FAIL — `quota_gate()` rejects the unexpected `kind`/`engine`/`model_id`/`profile_id` keyword arguments with a `TypeError`.

- [ ] **Step 3: Implement**

In `apps/api_gateway/app/services/quota/gate.py`, add the import next to the existing ones:

```python
from app.services.usage.recorder import record_usage
```

(There is no import cycle: `recorder` imports the model registry, attribution and pricing, none of which import `quota`.)

Add the unit map below `logger = logging.getLogger(__name__)`:

```python
# The unit an audit row carries per kind, matching what the metering call sites
# use for real usage so the two are comparable in a query.
_UNIT_BY_KIND = {"llm": "tokens", "embed": "tokens", "stt": "seconds", "tts": "chars"}
```

Replace `quota_gate` with:

```python
async def quota_gate(
    *, user_id: str, provider_id: str,
    kind: str = "", engine: str = "", model_id: str = "", profile_id: str = "",
) -> None:
    """Pre-flight: raise QuotaExceededError if any applicable enabled quota is at/over
    its limit for the current period. FAIL-OPEN: any other error logs and allows.

    kind/engine/model_id/profile_id describe the work being refused. When `kind`
    is given, a block also writes one `status="blocked"` audit row -- otherwise
    a refused request leaves no trace at all, since it does no work and so never
    appears in usage. They are optional so a caller with nothing meaningful to
    say (UsageEvent.kind is NOT NULL) simply gets no audit row.
    """
    try:
        quotas = await quota_store.list_enabled()
        for q in quotas:
            if not _applies(q, user_id, provider_id):
                continue
            spend = await current_spend(scope=q["scope"], scope_id=q["scope_id"], period=q["period"])
            if spend >= q["limit_usd"] > 0:
                raise QuotaExceededError(q["scope"], q["scope_id"], q["limit_usd"], spend, q["period"])
    except QuotaExceededError as exc:
        logger.warning(
            "quota block: user=%r provider=%r kind=%r engine=%r model=%r -- %s",
            user_id, provider_id, kind, engine, model_id, exc,
        )
        await _record_block(user_id, profile_id, kind, engine, model_id)
        raise
    except Exception as exc:  # noqa: BLE001 - fail-open, never deny service on a gate bug
        logger.warning("quota_gate failed open: %s", exc)


async def _record_block(user_id: str, profile_id: str, kind: str, engine: str, model_id: str) -> None:
    """Best-effort audit row for a refused request. Zero amount and zero cost:
    nothing was served, and a blocked row that carried cost would inflate the
    very spend that caused the block."""
    if not kind:
        return
    try:
        await record_usage(
            user_id=user_id or "", profile_id=profile_id or "", kind=kind,
            engine=engine or "", model_id=model_id or "",
            unit=_UNIT_BY_KIND.get(kind, ""), native_amount=0.0, status="blocked",
        )
    except Exception as exc:  # noqa: BLE001 - the block matters, the audit row does not
        logger.warning("quota block audit row failed: %s", exc)
```

Note the structure: the `except QuotaExceededError` clause must come BEFORE the generic `except Exception`, or a block would be swallowed as an internal error and the request allowed.

- [ ] **Step 4: Update the model comment**

In `apps/api_gateway/app/services/db/models.py`, the `UsageEvent.status` line currently ends with `# ok|error`. Change that comment to:

```python
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok|error|blocked
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/unit/test_quota_blocked_audit.py tests/unit/test_quota_gate.py tests/unit/test_quota_enforcement_core.py tests/unit/test_quota_enforcement_routes.py -q`
Expected: PASS. The three existing files must stay green — they call `quota_gate` with only `user_id`/`provider_id`, which is why the new parameters have defaults.

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/services/quota/gate.py apps/api_gateway/app/services/db/models.py tests/unit/test_quota_blocked_audit.py
git commit -m "feat(quota): write a status=blocked audit row when a request is refused"
```

---

### Task 2: Keep refused requests out of the usage summaries

**Files:**
- Modify: `apps/api_gateway/app/services/usage/query.py` (`summarize` and `summarize_for_user`)
- Test: `tests/unit/test_usage_query.py` (append)

**Interfaces:**
- Consumes: the `status="blocked"` rows Task 1 introduced.
- Produces: no signature change. Both summary functions now count only `status="ok"` rows.

**Why:** the Usage and My Usage tables are read as "what was served, and what it cost". A refused request served nothing. Counting it in `Requests` would make the dashboards disagree with reality the moment a quota starts biting — and this must be settled in the same change that starts writing those rows, not after they have accumulated.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_usage_query.py`:

```python
async def test_summaries_count_only_served_requests():
    """A status="blocked" row is an audit record, not usage: it served nothing.
    Counting it would inflate both Requests and the native amount."""
    await init_db()
    from app.services.usage.recorder import record_usage

    await record_usage(user_id="u-status", profile_id="p", kind="llm", engine="eng-status",
                       model_id="m", unit="tokens", native_amount=100)
    await record_usage(user_id="u-status", profile_id="p", kind="llm", engine="eng-status",
                       model_id="m", unit="tokens", native_amount=0, status="blocked")

    admin_rows = [r for r in await summarize("engine") if r["key"] == "eng-status"]
    assert len(admin_rows) == 1
    assert admin_rows[0]["count"] == 1
    assert admin_rows[0]["native_amount"] == 100.0

    mine = [r for r in await summarize_for_user("u-status") if r["engine"] == "eng-status"]
    assert len(mine) == 1
    assert mine[0]["count"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_usage_query.py -q`
Expected: FAIL — `assert 2 == 1`, both rows are counted.

- [ ] **Step 3: Implement**

In `apps/api_gateway/app/services/usage/query.py`, add the filter to both statements.

In `summarize`, after the `.group_by(column)` call:

```python
    ).group_by(column).where(UsageEvent.status == "ok")
```

In `summarize_for_user`, extend its existing `.where(...)`:

```python
    ).where(UsageEvent.user_id == user_id, UsageEvent.status == "ok").group_by(
        UsageEvent.kind, UsageEvent.engine, UsageEvent.model_id
    )
```

Then extend the module docstring's opening line so the exclusion is discoverable:

```python
"""Read-only aggregation over `usage_events` (T1's UsageEvent table).

Only `status="ok"` rows are counted: a `status="blocked"` row records a request
a quota refused, which served nothing and so is not usage. Those rows stay in
the table for audit and are queried directly when needed.
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/unit/test_usage_query.py tests/unit/test_usage_routes.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/usage/query.py tests/unit/test_usage_query.py
git commit -m "fix(usage): count only served requests in the usage summaries"
```

---

### Task 3: Resolve a coherent provider at every gate

**Files:**
- Modify: `apps/api_gateway/app/api/routes/stt.py` (the quota pre-flight, ~lines 62-77)
- Modify: `apps/api_gateway/app/api/routes/tts.py` (the quota pre-flight, ~lines 51-64)
- Modify: `apps/api_gateway/app/services/conversation/session.py` (`_run_turn`'s pre-flight, ~lines 420-441)
- Modify: `apps/api_gateway/app/services/memory/extractor.py` (`_quota_blocked`, ~lines 136-160)
- Modify: `apps/api_gateway/app/api/routes/conversation.py` (`/chat`'s gate call — audit context only)
- Test: `tests/unit/test_quota_provider_scope.py`

**Interfaces:**
- Consumes: `resolve_usage_model` (attribution) and the extended `quota_gate` from Task 1.
- Produces: no new API. Every gate now passes a `provider_id` derived from a coherent `(engine, model_id)` pair, plus the audit context (`kind`, `engine`, `model_id`, `profile_id`) Task 1 needs.

**The pattern, identical at every site:** resolve first, then look up.

```python
    engine, model_id = await resolve_usage_model(<kind>, <engine we know>, <model we know>)
    provider_id = ""
    try:
        entry = await model_registry_store.find(<kind>, engine, model_id)
        provider_id = (entry or {}).get("config", {}).get("provider_id", "") if entry else ""
    except Exception:  # noqa: BLE001 - a lookup failure must never block
        provider_id = ""
```

`resolve_usage_model` never raises and never guesses, so a blank stays blank and user/global quotas still apply — exactly the pre-existing behavior, just with the provider now found when it is knowable.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_quota_provider_scope.py`:

```python
"""A provider-scoped quota must fire on every entry point, not just /chat.

Every gate used to look the provider up with a blank model_id, which matches no
registry row, so `provider_id` stayed "" and `_applies()` skipped every
provider-scoped quota. Measured before this change: /transcribe, /synthesize and
the conversation turn all resolved to None.
"""

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.db.engine import init_db
from app.services.model_registry.store import model_registry_store
from app.services.quota.store import quota_store
from app.services.usage.recorder import record_usage


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


async def _provider_over_quota(kind: str, engine: str, model_id: str, provider_id: str) -> None:
    """Register a priced model on `provider_id` and push that provider over a $1 quota."""
    await init_db()
    quota_store.invalidate()
    await model_registry_store.create(
        kind, engine, model_id, f"{engine}/{model_id}",
        config={"provider_id": provider_id, "price": {"unit": "1M_tokens", "in": 10.0, "out": 0.0}},
    )
    await record_usage(user_id="someone-else", profile_id="", kind="llm", engine=engine,
                       model_id=model_id, unit="tokens", native_amount=1_000_000,
                       prompt_tokens=1_000_000)
    await quota_store.create(scope="provider", scope_id=provider_id, limit_usd=1.0, period="monthly")


async def test_transcribe_enforces_a_provider_quota(_with_password):
    """The STT route knows only its engine; the model comes from the registry."""
    await _provider_over_quota("stt", "qwencloud", "fun-asr", "prov-qwen")
    client = TestClient(app)
    resp = client.post(
        "/v1/transcribe",
        files={"audio": ("a.wav", b"RIFF0000WAVEfmt ", "audio/wav")},
        data={"engine": "qwencloud"},
    )
    assert resp.status_code == 429, resp.text
    assert "provider quota exceeded" in resp.json()["detail"]


async def test_synthesize_enforces_a_provider_quota(_with_password):
    await _provider_over_quota("tts", "vieneu", "vieneu", "prov-vn")
    client = TestClient(app)
    resp = client.post("/v1/synthesize", json={"text": "xin chao", "engine": "vieneu"})
    assert resp.status_code == 429, resp.text
    assert "provider quota exceeded" in resp.json()["detail"]


async def test_a_blocked_rest_request_leaves_an_audit_row(_with_password):
    """Task 1's audit row, reached through a real route."""
    from sqlalchemy import select

    from app.services.db.engine import db_session
    from app.services.db.models import UsageEvent

    await _provider_over_quota("tts", "vieneu", "vieneu", "prov-audit")
    client = TestClient(app)
    assert client.post("/v1/synthesize", json={"text": "hi", "engine": "vieneu"}).status_code == 429
    async with db_session() as s:
        rows = (await s.execute(select(UsageEvent))).scalars().all()
    blocked = [r for r in rows if r.status == "blocked"]
    assert len(blocked) == 1
    assert blocked[0].kind == "tts" and blocked[0].engine == "vieneu"


async def test_an_unrelated_provider_quota_does_not_block(_with_password):
    """The guard against over-correcting: resolving a provider must not make
    every provider's quota apply to every request."""
    await _provider_over_quota("tts", "vieneu", "vieneu", "prov-other")
    await model_registry_store.create(
        "tts", "edge_tts", "edge_tts", "Edge", config={"provider_id": "prov-innocent"},
    )
    client = TestClient(app)
    resp = client.post("/v1/synthesize", json={"text": "hi", "engine": "edge_tts"})
    assert resp.status_code != 429, "edge_tts is on a different provider and must not be blocked"
```

Note on the STT test: `/v1/transcribe` needs a registered `qwencloud` STT provider to get past the gate on the happy path, but the gate runs BEFORE the provider is fetched, so a 429 is returned without any provider work. If the route rejects the tiny WAV before reaching the gate, move the assertion to check that the 429 happens for a well-formed but silent WAV built with `pcm16_to_wav_bytes(b"\x00\x00" * 1600, sample_rate=16000)` from `app.core.audio` — the same fixture `tests/unit/test_routes_usage_metering.py` uses.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_quota_provider_scope.py -q`
Expected: FAIL — the transcribe/synthesize tests return 200 instead of 429, because `provider_id` resolves to `""` and the provider-scoped quota is skipped.

- [ ] **Step 3: Fix the STT route**

In `apps/api_gateway/app/api/routes/stt.py`, replace the pre-flight block (the comment starting "Quota pre-flight" through the `quota_gate` call) with:

```python
    # Quota pre-flight: block BEFORE the provider does any work. The route knows
    # the engine and maybe a model; resolve_usage_model turns that into the pair
    # the registry actually holds, so a provider-scoped quota can match. A blank
    # result still leaves user/global quotas enforced.
    from app.services.model_registry.store import model_registry_store
    from app.services.quota.gate import quota_gate, QuotaExceededError
    from app.services.usage.attribution import resolve_usage_model

    usage_engine, usage_model_id = await resolve_usage_model("stt", payload.engine, payload.model or "")
    provider_id = ""
    try:
        entry = await model_registry_store.find("stt", usage_engine, usage_model_id)
        provider_id = (entry or {}).get("config", {}).get("provider_id", "") if entry else ""
    except Exception:  # noqa: BLE001 - a registry hiccup must never block a request
        provider_id = ""
    try:
        await quota_gate(
            user_id=current_user_id(request) or "", provider_id=provider_id,
            kind="stt", engine=usage_engine, model_id=usage_model_id,
        )
    except QuotaExceededError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
```

This also removes the stale comment that said "model_id is not resolved per-request for STT (see the record_usage comment below)" — that comment pointed at a comment deleted in an earlier branch, and the statement is no longer true.

- [ ] **Step 4: Fix the TTS route**

In `apps/api_gateway/app/api/routes/tts.py`, replace its pre-flight block with:

```python
    # Quota pre-flight: block BEFORE the provider does any work. See the STT
    # route for why the model is resolved before the provider lookup.
    from app.services.model_registry.store import model_registry_store
    from app.services.quota.gate import quota_gate, QuotaExceededError
    from app.services.usage.attribution import resolve_usage_model

    usage_engine, usage_model_id = await resolve_usage_model("tts", payload.engine, payload.model_id or "")
    provider_id = ""
    try:
        entry = await model_registry_store.find("tts", usage_engine, usage_model_id)
        provider_id = (entry or {}).get("config", {}).get("provider_id", "") if entry else ""
    except Exception:  # noqa: BLE001 - a registry hiccup must never block a request
        provider_id = ""
    try:
        await quota_gate(
            user_id=current_user_id(request) or "", provider_id=provider_id,
            kind="tts", engine=usage_engine, model_id=usage_model_id,
        )
    except QuotaExceededError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
```

- [ ] **Step 5: Fix the conversation core's pre-flight**

In `apps/api_gateway/app/services/conversation/session.py`'s `_run_turn`, replace the `provider_id` resolution block and the `quota_gate` call with:

```python
        provider_id = ""
        try:
            llm_engine, llm_model = await resolve_usage_model(
                "llm",
                (self.profile.llm.engine if self.profile else "") or "",
                (self.profile.llm.model if self.profile else "") or "",
            )
            entry = await model_registry_store.find("llm", llm_engine, llm_model)
            provider_id = (entry or {}).get("config", {}).get("provider_id", "") if entry else ""
        except Exception:  # noqa: BLE001 - provider_id resolution must never block the turn
            llm_engine, llm_model, provider_id = "", "", ""
        try:
            from app.services.quota.gate import QuotaExceededError, quota_gate

            await quota_gate(
                user_id=cfg.identity_user_id or "", provider_id=provider_id,
                kind="llm", engine=llm_engine, model_id=llm_model,
                profile_id=cfg.profile_name or "",
            )
        except QuotaExceededError as exc:
            # Mirror the existing STT-failure pattern: a plain "error" notice,
            # then return without running the turn at all.
            await self.emit("error", message=str(exc))
            return
```

`resolve_usage_model` is already imported in this file (it is used by `resolve_llm_pair`'s neighbours); if it is not, add `from app.services.usage.attribution import resolve_usage_model` next to the existing `resolve_llm_pair` import.

- [ ] **Step 6: Fix the memory gate**

In `apps/api_gateway/app/services/memory/extractor.py`'s `_quota_blocked`, replace the `provider_id` resolution and the `quota_gate` call with:

```python
        from app.services.model_registry.store import model_registry_store
        from app.services.quota.gate import QuotaExceededError, quota_gate
        from app.services.usage.attribution import resolve_usage_model

        usage_engine, usage_model = "", ""
        provider_id = ""
        try:
            usage_engine, usage_model = await resolve_usage_model(
                "llm", profile.llm.engine or "", model
            )
            entry = await model_registry_store.find("llm", usage_engine, usage_model)
            provider_id = (entry or {}).get("config", {}).get("provider_id", "") if entry else ""
        except Exception:  # noqa: BLE001 - never block memory on a lookup
            provider_id = ""
        try:
            await quota_gate(
                user_id=user_id or "", provider_id=provider_id,
                kind="llm", engine=usage_engine, model_id=usage_model,
                profile_id=profile.name,
            )
        except QuotaExceededError as exc:
            logger.warning("memory extraction skipped for %s: %s", profile.name, exc)
            return True
        except Exception as exc:  # noqa: BLE001 - fail-open, same as quota_gate itself
            logger.warning("memory quota check failed open for %s: %s", profile.name, exc)
        return False
```

- [ ] **Step 7: Give `/chat`'s gate its audit context**

`/chat` already resolves coherently. It only needs the new parameters so its blocks are audited too. In `apps/api_gateway/app/api/routes/conversation.py`, change the `quota_gate` call to:

```python
        await quota_gate(
            user_id=caller_id or "", provider_id=provider_id,
            kind="llm", engine=quota_engine, model_id=quota_model_id,
            profile_id=profile or "",
        )
```

- [ ] **Step 8: Run the tests**

Run: `.venv/bin/python -m pytest tests/unit/test_quota_provider_scope.py tests/unit/test_quota_enforcement_routes.py tests/unit/test_quota_enforcement_core.py tests/unit/test_memory_quota_gate.py tests/unit/test_routes_usage_metering.py -q`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add apps/api_gateway/app/api/routes/stt.py apps/api_gateway/app/api/routes/tts.py apps/api_gateway/app/api/routes/conversation.py apps/api_gateway/app/services/conversation/session.py apps/api_gateway/app/services/memory/extractor.py tests/unit/test_quota_provider_scope.py
git commit -m "fix(quota): resolve a coherent provider at every gate, audit every block"
```

---

### Task 4: Gate the livehost turns

**Files:**
- Modify: `apps/api_gateway/app/api/routes/livehost.py` (`_run_voice_turn` ~line 350, `_run_social_turn` ~line 393)
- Test: `tests/unit/test_livehost_quota_gate.py`

**Interfaces:**
- Consumes: `quota_gate`, `QuotaExceededError`, `resolve_usage_model`, `model_registry_store`.
- Produces: no new API. Both livehost turn paths refuse to run when a quota is over limit, and say so on the socket.

**Why this is a hole and not a nicety:** `grep -n quota apps/api_gateway/app/api/routes/livehost.py` returns nothing today. Every other conversation entry point gates; this one runs STT + LLM + TTS unchecked, so an over-limit user only has to use it.

**Placement:** resolve once per connection (the profile and engines are fixed for the connection's lifetime), then check per turn — spend changes between turns, so a per-connection check would let an over-limit session run forever.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_livehost_quota_gate.py`:

```python
"""livehost had no quota gate at all: an over-limit user could simply use this
endpoint instead of the gated ones."""

import pytest

from app.services.db.engine import init_db
from app.services.model_registry.store import model_registry_store
from app.services.quota.store import quota_store
from app.services.usage.recorder import record_usage


@pytest.mark.asyncio
async def test_livehost_module_gates_its_turns():
    """A structural check: both turn entry points must reach quota_gate.

    livehost's turn functions are closures over a live WebSocket, so driving
    them end to end needs the full socket harness. This asserts the wiring
    exists; the gate's own behavior is covered by tests/unit/test_quota_gate.py
    and the REST paths in tests/unit/test_quota_provider_scope.py.
    """
    import inspect

    from app.api.routes import livehost

    source = inspect.getsource(livehost)
    assert "quota_gate" in source, "livehost must gate its turns"
    # Both paths, not just the voice one.
    voice = source.split("async def _run_voice_turn")[1].split("async def run_voice_turn")[0]
    social = source.split("async def _run_social_turn")[1].split("async def run_social_turn")[0]
    assert "_quota_blocked" in voice, "the voice turn must check the quota"
    assert "_quota_blocked" in social, "the social turn must check the quota"


@pytest.mark.asyncio
async def test_livehost_quota_helper_blocks_when_over_limit():
    """The helper both turn paths call, exercised directly."""
    from app.api.routes.livehost import _quota_blocked_for

    await init_db()
    quota_store.invalidate()
    await model_registry_store.create(
        "llm", "OA", "lh-model", "LH",
        config={"provider_id": "prov-lh", "price": {"unit": "1M_tokens", "in": 10.0, "out": 0.0}},
    )
    await record_usage(user_id="u-lh", profile_id="", kind="llm", engine="OA",
                       model_id="lh-model", unit="tokens", native_amount=1_000_000,
                       prompt_tokens=1_000_000)
    await quota_store.create(scope="user", scope_id="u-lh", limit_usd=1.0, period="monthly")

    blocked, message = await _quota_blocked_for(
        user_id="u-lh", profile_name="p", engine="OA", model="lh-model",
    )
    assert blocked is True
    assert "quota exceeded" in message

    under, message2 = await _quota_blocked_for(
        user_id="u-nobody", profile_name="p", engine="OA", model="lh-model",
    )
    assert under is False and message2 == ""


@pytest.mark.asyncio
async def test_livehost_quota_helper_fails_open():
    from app.api.routes import livehost

    await init_db()
    quota_store.invalidate()

    async def boom(**kwargs):
        raise RuntimeError("quota subsystem down")

    original = livehost.quota_gate
    livehost.quota_gate = boom
    try:
        blocked, message = await livehost._quota_blocked_for(
            user_id="u-x", profile_name="p", engine="OA", model="m",
        )
        assert blocked is False and message == ""
    finally:
        livehost.quota_gate = original
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_livehost_quota_gate.py -q`
Expected: FAIL — `assert "quota_gate" in source` fails, and `_quota_blocked_for` does not exist.

- [ ] **Step 3: Add the helper**

In `apps/api_gateway/app/api/routes/livehost.py`, add these module-level imports next to the existing ones:

```python
from app.services.quota.gate import QuotaExceededError, quota_gate
from app.services.usage.attribution import resolve_usage_model
```

(The test monkeypatches `livehost.quota_gate`, which only works if the name is bound at module level — do not import it inside the function here.)

Add this module-level helper above the websocket handler:

```python
async def _quota_blocked_for(
    *, user_id: str, profile_name: str, engine: str, model: str
) -> tuple[bool, str]:
    """(blocked, message) for one livehost turn.

    Returns the message rather than raising so each turn path can report it the
    way that path reports its own failures. Fail-open: only a genuine
    QuotaExceededError blocks; anything else logs and allows, matching
    quota_gate's own contract.
    """
    try:
        usage_engine, usage_model = await resolve_usage_model("llm", engine or "", model or "")
        provider_id = ""
        try:
            entry = await model_registry_store.find("llm", usage_engine, usage_model)
            provider_id = (entry or {}).get("config", {}).get("provider_id", "") if entry else ""
        except Exception:  # noqa: BLE001 - a registry hiccup must never block a turn
            provider_id = ""
        await quota_gate(
            user_id=user_id or "", provider_id=provider_id,
            kind="llm", engine=usage_engine, model_id=usage_model,
            profile_id=profile_name or "",
        )
    except QuotaExceededError as exc:
        return True, str(exc)
    except Exception as exc:  # noqa: BLE001 - fail-open
        logger.warning("livehost quota check failed open: %s", exc)
    return False, ""
```

`model_registry_store` is already imported in this module; if it is not, add `from app.services.model_registry.store import model_registry_store`.

- [ ] **Step 4: Check the quota in both turn paths**

In `_run_voice_turn`, immediately after `await send("processing", turn=turn)`:

```python
            blocked, quota_message = await _quota_blocked_for(
                user_id=identity.user_id or "", profile_name=profile_name or "",
                engine=(profile.llm.engine if profile else "") or "",
                model=llm_model or (profile.llm.model if profile else "") or "",
            )
            if blocked:
                await send("error", message=quota_message)
                await send("turn_done", turn=turn)
                return
```

In `_run_social_turn`, immediately after its `await send("social_reply", ...)` call, add the same block but ending with `return` alone — check how that function signals completion (it does not send `turn_done` the way the voice path does; match whatever the surrounding code does on its own early-return paths, and if it has none, just `return` after the error send).

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/unit/test_livehost_quota_gate.py -q` then `.venv/bin/python -m pytest tests/unit -q -k livehost`
Expected: PASS both. Then `.venv/bin/python -c "import app.main"` to confirm the new module-level imports introduce no cycle.

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/api/routes/livehost.py tests/unit/test_livehost_quota_gate.py
git commit -m "fix(quota): gate livehost voice and social turns"
```

---

### Task 5: Reject quota rows the gate would ignore

**Files:**
- Modify: `apps/api_gateway/app/api/routes/quotas.py`
- Modify: `apps/api_gateway/app/static/index.html` (the two quota limit inputs)
- Modify: `apps/api_gateway/app/static/js/quotas.js` (the detail-edit limit input, ~line 52)
- Test: `tests/unit/test_quotas_routes.py` (append)

**Interfaces:**
- Produces: `POST /v1/quotas` and `PATCH /v1/quotas/{id}` reject, with 400 and an explanatory message: a blank `scope_id` on a `user`/`provider` scope; a `limit_usd` that is not `> 0`; and a duplicate of an existing `(scope, scope_id, period)`. A `global` scope has its `scope_id` normalized to `""`.

**The four states currently accepted, and why each is wrong:**
- `scope=user` with a blank `scope_id` matches `user_id == ""`, the shared-device bucket — so a quota an admin thought applied to a person silently applies to every anonymous request instead.
- `limit_usd = 0` reads as "allow nothing" but the gate's `spend >= limit > 0` condition means it never fires: the row is silently unlimited, the exact opposite. Rejecting it is safer than changing the gate, and "block everything" is properly expressed by disabling the model or provider.
- A negative limit is the same silent no-op.
- Two rows for the same `(scope, scope_id, period)` both apply; the strictest wins, so the other is dead config an admin will misread.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_quotas_routes.py`:

```python
def test_create_rejects_a_scoped_quota_with_no_scope_id(client, _with_password):
    """A blank scope_id on a user scope matches the shared-device bucket, not
    the person the admin had in mind."""
    _login_admin(client, "q-scopeid")
    for scope in ("user", "provider"):
        resp = client.post("/v1/quotas", json={"scope": scope, "scope_id": "  ", "limit_usd": 5.0})
        assert resp.status_code == 400, f"{scope}: {resp.text}"
        assert "scope_id" in resp.json()["detail"]


def test_global_scope_normalizes_its_scope_id_away(client, _with_password):
    _login_admin(client, "q-global")
    created = client.post(
        "/v1/quotas", json={"scope": "global", "scope_id": "ignored", "limit_usd": 5.0},
    ).json()["data"]
    assert created["scope_id"] == ""


def test_create_rejects_a_limit_that_can_never_fire(client, _with_password):
    """The gate requires `limit_usd > 0`, so 0 and negatives are silently
    unlimited -- the opposite of what an admin setting 0 intends."""
    _login_admin(client, "q-limit")
    for bad in (0, -5.0):
        resp = client.post("/v1/quotas", json={"scope": "global", "limit_usd": bad})
        assert resp.status_code == 400, f"{bad}: {resp.text}"
        assert "greater than 0" in resp.json()["detail"]


def test_create_rejects_a_duplicate_scope(client, _with_password):
    _login_admin(client, "q-dup")
    first = client.post(
        "/v1/quotas", json={"scope": "user", "scope_id": "u1", "limit_usd": 5.0, "period": "monthly"},
    )
    assert first.status_code == 200
    dup = client.post(
        "/v1/quotas", json={"scope": "user", "scope_id": "u1", "limit_usd": 9.0, "period": "monthly"},
    )
    assert dup.status_code == 400
    assert "already" in dup.json()["detail"]

    # A different period for the same scope is a legitimate second quota.
    other = client.post(
        "/v1/quotas", json={"scope": "user", "scope_id": "u1", "limit_usd": 50.0, "period": "total"},
    )
    assert other.status_code == 200, other.text


def test_patch_is_validated_the_same_way(client, _with_password):
    _login_admin(client, "q-patch")
    created = client.post(
        "/v1/quotas", json={"scope": "user", "scope_id": "u2", "limit_usd": 5.0},
    ).json()["data"]

    assert client.patch(f"/v1/quotas/{created['id']}", json={"limit_usd": 0}).status_code == 400
    assert client.patch(f"/v1/quotas/{created['id']}", json={"scope_id": ""}).status_code == 400

    # Editing a row into a collision with another row is also a duplicate.
    other = client.post(
        "/v1/quotas", json={"scope": "user", "scope_id": "u3", "limit_usd": 5.0},
    ).json()["data"]
    collide = client.patch(f"/v1/quotas/{other['id']}", json={"scope_id": "u2"})
    assert collide.status_code == 400
    assert "already" in collide.json()["detail"]

    # A no-op edit of an unrelated field must still be allowed.
    assert client.patch(f"/v1/quotas/{created['id']}", json={"enabled": False}).status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_quotas_routes.py -q`
Expected: FAIL — every new test gets 200 where it expects 400.

- [ ] **Step 3: Implement the validation**

In `apps/api_gateway/app/api/routes/quotas.py`, add below `_validate_period`:

```python
def _normalize_scope_id(scope: str, scope_id: str) -> str:
    """A scoped quota needs something to scope to; a global one must not carry a
    stray id that would make two identical-looking rows differ."""
    scope_id = (scope_id or "").strip()
    if scope == "global":
        return ""
    if not scope_id:
        raise HTTPException(
            status_code=400,
            detail=f"scope '{scope}' needs a scope_id (the {scope} it applies to)",
        )
    return scope_id


def _validate_limit(limit_usd: float) -> None:
    """The gate only fires on `spend >= limit_usd > 0`, so a limit of 0 or less
    is silently unlimited -- the opposite of what it looks like."""
    if limit_usd is None or limit_usd <= 0:
        raise HTTPException(
            status_code=400,
            detail=(
                f"limit_usd must be greater than 0 (got {limit_usd}); "
                "to allow no spend at all, disable the model or provider instead"
            ),
        )


async def _reject_duplicate(scope: str, scope_id: str, period: str, exclude_id: str = "") -> None:
    """Two rows for the same (scope, scope_id, period) both apply and the
    strictest silently wins, leaving the other as config an admin will misread."""
    for existing in await quota_store.list_all():
        if existing["id"] == exclude_id:
            continue
        if (existing["scope"], existing["scope_id"], existing["period"]) == (scope, scope_id, period):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"a {period} quota for {scope} '{scope_id}' already exists "
                    f"(id {existing['id']}) -- edit that one instead"
                ),
            )
```

Replace `create_quota` with:

```python
@router.post("")
async def create_quota(payload: CreateQuotaRequest) -> dict:
    _validate_scope(payload.scope)
    _validate_period(payload.period)
    scope_id = _normalize_scope_id(payload.scope, payload.scope_id)
    _validate_limit(payload.limit_usd)
    await _reject_duplicate(payload.scope, scope_id, payload.period)
    created = await quota_store.create(
        scope=payload.scope, scope_id=scope_id, limit_usd=payload.limit_usd,
        period=payload.period, enabled=payload.enabled,
    )
    return {"success": True, "data": created}
```

Replace `update_quota` with:

```python
@router.patch("/{quota_id}")
async def update_quota(quota_id: str, payload: UpdateQuotaRequest) -> dict:
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    existing = await quota_store.get(quota_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"quota '{quota_id}' not found")

    # Validate the row as it will BE, not just the fields that changed: a patch
    # can move a row into an invalid or colliding state one field at a time.
    merged = {**existing, **fields}
    _validate_scope(merged["scope"])
    _validate_period(merged["period"])
    merged["scope_id"] = _normalize_scope_id(merged["scope"], merged["scope_id"])
    _validate_limit(merged["limit_usd"])
    await _reject_duplicate(
        merged["scope"], merged["scope_id"], merged["period"], exclude_id=quota_id,
    )
    if "scope_id" in fields or fields.get("scope") == "global":
        fields["scope_id"] = merged["scope_id"]

    updated = await quota_store.set_fields(quota_id, **fields)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"quota '{quota_id}' not found")
    return {"success": True, "data": updated}
```

Note the `PATCH` subtlety the tests cover: `fields` drops `None` values, so `{"scope_id": ""}` survives as an empty string (it is not `None`) and correctly trips the blank-scope_id check, while an untouched field falls back to `existing`.

- [ ] **Step 4: Make the UI stop offering an invalid value**

In `apps/api_gateway/app/static/index.html`, the quota add form's limit input currently allows 0. Change its attributes to `step="0.01" min="0.01"` and its placeholder to `e.g. 25.00`. In `apps/api_gateway/app/static/js/quotas.js` (~line 52) the detail-edit input has `step="0.01" min="0"` — change `min="0"` to `min="0.01"`.

Then verify: `node --check apps/api_gateway/app/static/js/quotas.js` and `grep -nE '[‘’“”]' apps/api_gateway/app/static/js/quotas.js apps/api_gateway/app/static/index.html` (must find nothing).

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/unit/test_quotas_routes.py tests/unit/test_quota_store.py -q`
Expected: PASS. If an existing test in either file creates a quota in one of the now-rejected shapes (a blank `scope_id`, a `limit_usd` of 0), update that test to a valid shape — but only the setup values, never an assertion about behavior.

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/api/routes/quotas.py apps/api_gateway/app/static/index.html apps/api_gateway/app/static/js/quotas.js tests/unit/test_quotas_routes.py
git commit -m "fix(quota): reject quota rows the gate would silently ignore"
```

---

### Task 6: Full-suite gate + docs

**Files:**
- Modify: `docs/superpowers/specs/2026-07-23-provider-management-usage-quota-design.md`

- [ ] **Step 1: Run the whole backend suite**

Run: `.venv/bin/python -m pytest tests/unit -q`
Expected: all pass. The baseline before this plan was 1341. If something unrelated fails, clear stale bytecode (`find apps tests -name __pycache__ -prune -exec rm -rf {} +`) and re-run; if a real failure remains, STOP and report it rather than committing.

- [ ] **Step 2: Verify the app imports and the JS parses**

```bash
.venv/bin/python -c "import app.main"
node --check apps/api_gateway/app/static/js/quotas.js
```
Expected: no output from either.

- [ ] **Step 3: Prove the headline fix end to end**

The plan exists because provider-scoped quotas did not fire. Confirm they now do, against a scratch database:

```bash
.venv/bin/python - <<'PY'
import asyncio, os, tempfile
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + tempfile.mktemp(suffix=".db")
from app.services.db.engine import init_db
from app.services.model_registry.store import model_registry_store as reg
from app.services.usage.attribution import resolve_usage_model

async def main():
    await init_db()
    await reg.create("stt", "qwencloud", "fun-asr", "Fun", config={"provider_id": "prov-qwen"})
    await reg.create("tts", "vieneu", "vieneu", "VieNeu", config={"provider_id": "prov-vn"})
    for kind, engine in (("stt", "qwencloud"), ("tts", "vieneu")):
        e, m = await resolve_usage_model(kind, engine, "")
        entry = await reg.find(kind, e, m)
        pid = (entry or {}).get("config", {}).get("provider_id", "")
        print(f"  {kind}/{engine}: resolved ({e}, {m}) -> provider {pid or 'NONE'}")
asyncio.run(main())
PY
```
Expected: both lines name a provider (`prov-qwen`, `prov-vn`), where before this plan both printed `NONE`.

- [ ] **Step 4: Record what shipped**

Append to `docs/superpowers/specs/2026-07-23-provider-management-usage-quota-design.md`:

```markdown
## 14. Quota enforcement gaps đã đóng (2026-07-26)

Theo plan `plans/2026-07-26-quota-enforcement-gaps.md`:
- **Quota theo provider giờ mới thực sự chạy.** Trước đó mọi gate tra
  `provider_id` bằng `find(kind, engine, "")` nên không khớp row nào và
  `_applies()` bỏ qua toàn bộ quota scope=provider (đo được: /transcribe,
  /synthesize, và turn hội thoại đều ra NONE). Nay mọi gate đi qua
  `resolve_usage_model` trước, giống `/chat`.
- **livehost đã có gate.** Trước đó `grep quota` trong `routes/livehost.py`
  không ra gì — cả hai đường turn (voice + social) chạy STT/LLM/TTS không kiểm.
- **Audit row `status="blocked"`** (spec §7) do chính `quota_gate` ghi, với
  `cost_usd = 0` và `native_amount = 0` để không bao giờ tự cộng vào spend đã
  gây ra block. Summary usage chỉ đếm `status="ok"`.
- **Validate quota:** `scope_id` bắt buộc với scope user/provider (rỗng sẽ khớp
  bucket thiết bị chung), `limit_usd > 0` (0 và số âm bị gate bỏ qua, tức là
  "không giới hạn" — ngược ý admin), và chặn trùng `(scope, scope_id, period)`.

Còn lại (xem audit 2026-07-26): metering + gate cho `POST /v1/tts/stream` và
`WS /v1/stt/stream`; `profile_id=""` ở REST; hiển thị spend/limit trên tab
Quotas và My Usage; client chưa xử lý 429 riêng; tab Pricing vẫn liệt kê row
sentinel; rollup `usage_counters` và prune `usage_events`.
```

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-07-23-provider-management-usage-quota-design.md
git commit -m "docs: record the quota enforcement gaps closed"
```

- [ ] **Step 6: Report, do not push**

Report the final test count and the commit list. `main` auto-deploys and this branch is not merged — merging is the user's call.

---

## Self-Review

**1. Coverage of the audit items this plan claims:**
- *P0 — provider-scoped quotas never fire* → Task 3 (the four gates) plus Task 4 (livehost, which had no gate at all to fix). Task 6 Step 3 re-runs the measurement that motivated the plan.
- *P1#1 — quota rows saved in states the gate ignores* → Task 5, one test per measured state (blank scope_id, limit 0, negative limit, duplicate), plus PATCH parity.
- *P1#2 — no `status="blocked"` audit row* → Task 1, with Task 2 settling what the new rows mean for the dashboards in the same breath, and Task 3 Step 7 wiring the last call site's context.

**2. Placeholder scan:** every step carries literal code or a literal command. Two places name a judgment the implementer must make rather than a blank: Task 3 Step 1's note on the STT fixture (with the exact fallback fixture to use), and Task 4 Step 4's instruction to match `_run_social_turn`'s own early-return convention (with what to do if it has none). Task 5 Step 5 pre-authorizes fixing setup values in existing tests while forbidding assertion changes.

**3. Type consistency:** `quota_gate(*, user_id, provider_id, kind="", engine="", model_id="", profile_id="")` is defined in Task 1 and called with exactly those names in Tasks 3, 4. `_record_block(user_id, profile_id, kind, engine, model_id)` is internal to Task 1. `_quota_blocked_for(*, user_id, profile_name, engine, model) -> tuple[bool, str]` is defined and consumed within Task 4. `resolve_usage_model(kind, engine, model_id) -> (engine, model_id)` is pre-existing and used identically in Tasks 3 and 4. `_normalize_scope_id`, `_validate_limit`, `_reject_duplicate` are defined and used within Task 5.

**4. Ordering:** Task 1 before 3 and 4 (they pass the parameters it adds). Task 2 immediately after 1, so blocked rows never reach a dashboard uncounted-for. Task 5 is independent of 1-4 and could run any time. Task 4's structural test asserts on `_run_voice_turn`/`_run_social_turn` source, so it must run after those functions exist in their edited form — i.e. its own steps are self-contained.

**5. One deliberate deviation from the usual test style, flagged for the reviewer:** Task 4's first test inspects module source rather than driving a WebSocket. livehost's turn functions are closures over a live socket and its harness is heavy; the behavioral coverage lives in the second test (the extracted helper, exercised directly) and in Task 3's REST tests of the same gate. If a reviewer judges the source-inspection test to be of low value, deleting it is acceptable — the helper test is the one that matters.
