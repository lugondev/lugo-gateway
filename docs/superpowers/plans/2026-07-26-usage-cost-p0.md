# Usage/Cost P0 — Model Attribution + Pricing UI + Memory Metering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing usage/cost/quota feature produce correct, non-zero costs: every usage row names the model that was actually billed (no more `(none)`), admins get a validated UI to enter per-model prices, and the memory subsystem stops spending money invisibly.

**Architecture:** Three phases, in dependency order.
- **Phase 1 (Tasks 1-5) — attribution.** `record_usage` currently stores whatever `model_id` the caller passed, and several callers pass `""`. A `usage/attribution.py` resolver fills blanks from the STT catalog default / the engine's single non-sentinel registry row / the active LLM entry, and `record_usage` calls it for *every* row so no call site can regress. A startup migration backfills legacy blank rows where the model is provable; both usage views label the rest honestly.
- **Phase 2 (Tasks 6-9) — pricing.** `usage/price_schema.py` becomes the write-time gate for `config["price"]`, exposed via a bulk `GET/PATCH /v1/model_registry/prices` API and a new admin "Pricing" tab. `embed` becomes a first-class registry kind so embedding models are priceable.
- **Phase 3 (Tasks 10-13) — memory metering.** The four unmetered paid call sites in `services/memory/` get `record_usage`, and post-session memory work gets a `quota_gate` that skips silently when over limit.

**Phase 1 must land before Phase 2 matters:** a usage row recorded as `("tts", "vieneu", "")` can never match the priced registry row `("tts", "vieneu", "vieneu")`, so pricing TTS would still resolve to $0 without the attribution fix.

**Tech Stack:** Python 3.12 (FastAPI, SQLAlchemy async, pytest), vanilla ES-module JS for the admin static UI.

## Global Constraints

- **Python:** always `.venv/bin/python` (the venv is 3.12; the system Python lacks the ML wheels).
- **Test scope:** run `tests/unit` of this repo only (`.venv/bin/python -m pytest tests/unit -q`). Do not run submodule test suites.
- **Never push.** `main` auto-deploys to production. Commit locally only; the user decides when to push.
- **Git identity:** commits are authored as `lugondev <lugondev@gmail.com>`.
- **Branch:** do all work on `feat/usage-cost-p0` (create it off `main` before Task 1). Other sessions share this working tree — do not switch branches mid-task.
- **ASCII quotes only** in any `.js` / `.html` you touch. A previous session corrupted static UI files with smart/curly quotes (`’ “ ”`), and `node --check` does *not* catch them inside string literals. After every JS/HTML edit, verify with the Read tool that quotes are straight.
- **Metering must never raise into a caller.** `record_usage` already swallows its own errors; every new call site must additionally wrap arg-building in `try/except Exception` + `logger.warning`, matching `session.py:_record_llm_usage` (`apps/api_gateway/app/services/conversation/session.py:377-399`).
- **The quota gate is fail-open.** `quota_gate` only raises `QuotaExceededError`; any internal error logs and allows. Never add a code path where a gate bug denies service.
- **Prices are per-model, optional.** A model with no price keeps recording usage at `cost_usd = 0.0`. That is expected behavior, not a bug — do not invent default prices.
- **Never rewrite historical `cost_usd`.** Task 4 backfills `model_id` only. Recomputing past costs from prices entered today would fabricate billing history; if the user wants that, it is a separate decision.

---

## Reference: how the existing pieces fit

Read this before Task 1; it is the context every task assumes.

- `apps/api_gateway/app/services/usage/recorder.py:14` — `record_usage(*, user_id, profile_id, kind, engine, model_id, unit, native_amount, prompt_tokens=None, completion_tokens=None, request_id=None, status="ok")`. Resolves `provider_id` + `price` via `model_registry_store.find(kind, engine, model_id)`, so **a row is only costed when a registry entry exists at that exact `(kind, engine, model_id)` key** — which is why a blank `model_id` is a cost bug, not just a display bug.
- `apps/api_gateway/app/services/usage/pricing.py:12` — `compute_cost(price, prompt_tokens, completion_tokens, native_amount)`. `"1M_tokens"` uses `in`/`out` against token counts; `"minute"` uses `rate` against `native_amount / 60`; `"1k_chars"` uses `rate` against `native_amount / 1000`. **Anything unrecognized returns 0.0 silently** — the silence Task 6 fixes at write time.
- `apps/api_gateway/app/services/quota/gate.py:55` — `quota_gate(*, user_id: str, provider_id: str) -> None`, raises `QuotaExceededError` (same module).
- `apps/api_gateway/app/services/db/models.py:136` — `UsageEvent`. `kind` is `String(8)`, so `"embed"` fits with no migration. `usage/query.py` groups by `kind` generically, so a new kind appears in the Usage dashboard automatically.
- `apps/api_gateway/app/services/model_registry/store.py` — `list_all()` returns dicts with `id, kind, engine, model_id, label, enabled, stage, api_key, base_url, config, is_default`; `find(kind, engine, model_id)`; `find_default(kind)`; `find_enabled(kind, engine=None)`; `set_fields(entry_id, **fields)` writes through the cache. **`model_id == ""` rows are engine-config sentinels, not real models** — every lookup in this plan must exclude them.
- `apps/api_gateway/app/services/stt/model_catalog.py` — `resolve_default_stt_model(engine)` is the canonical "what model does this STT engine actually load" answer; `livehost.py:136` and `session.py:211` already use it.
- `apps/api_gateway/app/services/conversation/responder.py:36-45` — `_active_llm_entry()` = `find_default("llm")` filtered to `enabled`; `get_active_llm_model()` returns its `model_id`. `OpenAICompatResponder.model` (line 185) is the model string actually sent to the provider — the ground truth for LLM attribution.
- Test style: newer files (`tests/unit/test_usage_recorder.py`) are marker-free async with `await init_db()`; older memory files (`tests/unit/test_memory_extractor.py`) use `@pytest.mark.asyncio`. **Match the file you are editing**; for new files use the marker-free style.

### Measured state of the production data (2026-07-26, `data/app.db`)

This is what the `(none)` rows in the screenshot actually are. Task 4's backfill rules were derived from it.

```
usage_events GROUP BY kind, engine, model_id        registry candidates (model_id != "")
llm  ''              ''                 27 rows    engine is blank too -> unprovable
tts  omnivoice       ''                172 rows    omnivoice/omnivoice          -> provable
tts  vieneu          ''                 71 rows    vieneu/vieneu                -> provable
stt  qwen3_asr_gguf  ''                 24 rows    .../qwen3-asr-1.7b-q8_0.gguf -> provable
stt  http_stt        ''                  1 row     Qwen/Qwen3-ASR-0.6B          -> provable
stt  qwen3_asr_or    ''                  1 row     qwen/qwen3-asr-flash-...     -> provable
stt  whisper_or      ''                  1 row     openai/whisper-large-v3-...  -> provable
stt  qwencloud       ''                  9 rows    fun-asr + qwen3-asr-flash    -> AMBIGUOUS, skip
stt  whisper         ''                  1 row     large-v3 + large-v3-turbo    -> AMBIGUOUS, skip
```

270 of 307 blank rows are provably attributable; 37 are not and must be labeled, not guessed.

---

## Phase 1 — Model attribution

### Task 1: Attribution resolver

**Files:**
- Create: `apps/api_gateway/app/services/usage/attribution.py`
- Test: `tests/unit/test_usage_attribution.py`

**Interfaces:**
- Consumes: `model_registry_store` (`list_all`, `find_default`), `resolve_default_stt_model` (`app.services.stt.model_catalog`).
- Produces: `resolve_usage_model(kind: str, engine: str, model_id: str) -> tuple[str, str]` — returns `(engine, model_id)` with blanks filled where provable, never raising.

**Resolution rules (in order, first hit wins):**
1. Both non-blank → returned unchanged. This is the common path and must stay allocation-cheap.
2. `model_id` blank, `kind == "stt"`, `engine` known → `resolve_default_stt_model(engine)`.
3. `model_id` blank, `engine` non-blank → the engine's **single** non-sentinel registry row for that kind (enabled rows preferred; skip when 0 or 2+ candidates).
4. `model_id` blank, `engine` blank, `kind == "llm"` → the active default LLM entry's `(engine, model_id)`.
5. `engine` blank, `model_id` non-blank → reverse lookup: the single registry row of that kind with this `model_id` (enabled preferred).
6. Nothing provable → return the inputs untouched. A blank stays blank; guessing is worse than admitting.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_usage_attribution.py`:

```python
from app.services.db.engine import init_db
from app.services.model_registry.store import model_registry_store
from app.services.usage.attribution import resolve_usage_model


async def test_both_present_is_returned_unchanged():
    assert await resolve_usage_model("llm", "openai", "gpt-4o") == ("openai", "gpt-4o")


async def test_stt_blank_model_uses_the_engines_catalog_default(monkeypatch):
    await init_db()
    monkeypatch.setattr(
        "app.services.usage.attribution.resolve_default_stt_model",
        lambda engine: "large-v3-turbo" if engine == "whisper" else None,
    )
    assert await resolve_usage_model("stt", "whisper", "") == ("whisper", "large-v3-turbo")


async def test_blank_model_resolves_the_engines_single_registry_row():
    await init_db()
    await model_registry_store.create("tts", "vieneu-attr", "vieneu-attr", "VieNeu")
    assert await resolve_usage_model("tts", "vieneu-attr", "") == ("vieneu-attr", "vieneu-attr")


async def test_sentinel_config_rows_are_never_used_as_the_model():
    await init_db()
    # model_id="" is an engine-config sentinel, not a selectable model.
    await model_registry_store.create("stt", "sent-attr", "", "engine config")
    await model_registry_store.create("stt", "sent-attr", "real-model", "Real")
    assert await resolve_usage_model("stt", "sent-attr", "") == ("sent-attr", "real-model")


async def test_ambiguous_engine_stays_blank(monkeypatch):
    await init_db()
    monkeypatch.setattr(
        "app.services.usage.attribution.resolve_default_stt_model", lambda engine: None
    )
    await model_registry_store.create("stt", "amb-attr", "model-a", "A")
    await model_registry_store.create("stt", "amb-attr", "model-b", "B")
    # Two candidates: picking either would invent data.
    assert await resolve_usage_model("stt", "amb-attr", "") == ("amb-attr", "")


async def test_enabled_row_wins_over_disabled_when_resolving_an_engine():
    await init_db()
    await model_registry_store.create("tts", "pref-attr", "old-model", "Old", enabled=False)
    await model_registry_store.create("tts", "pref-attr", "new-model", "New", enabled=True)
    assert await resolve_usage_model("tts", "pref-attr", "") == ("pref-attr", "new-model")


async def test_blank_engine_and_model_for_llm_uses_the_active_default():
    await init_db()
    await model_registry_store.create(
        "llm", "def-attr", "default-model", "Default", is_default=True
    )
    assert await resolve_usage_model("llm", "", "") == ("def-attr", "default-model")


async def test_blank_engine_is_recovered_from_the_model_id():
    await init_db()
    await model_registry_store.create("llm", "rev-attr", "rev-model", "Rev")
    assert await resolve_usage_model("llm", "", "rev-model") == ("rev-attr", "rev-model")


async def test_unknown_engine_returns_the_inputs_untouched(monkeypatch):
    await init_db()
    monkeypatch.setattr(
        "app.services.usage.attribution.resolve_default_stt_model", lambda engine: None
    )
    assert await resolve_usage_model("stt", "nothing-registered", "") == ("nothing-registered", "")


async def test_never_raises_when_the_store_is_broken(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(model_registry_store, "list_all", boom)
    monkeypatch.setattr(model_registry_store, "find_default", boom)
    assert await resolve_usage_model("tts", "some-engine", "") == ("some-engine", "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_usage_attribution.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'app.services.usage.attribution'`

- [ ] **Step 3: Write the implementation**

Create `apps/api_gateway/app/services/usage/attribution.py`:

```python
"""Fill in the (engine, model_id) a usage row should be attributed to.

record_usage stores what the caller passed and prices the row by looking up
`find(kind, engine, model_id)`. Several call sites legitimately don't know the
model -- a REST /synthesize without model_id, a session whose profile pins no
LLM, a fast-path STT engine switch -- and used to record "". That blank is not
just an ugly "(none)" in the dashboard: it can never match the registry row
that carries the price, so those requests were structurally uncostable.

This module answers "which model actually served this?" from the same sources
the runtime used to pick it, and NEVER guesses: an engine with two candidate
models resolves to blank rather than to the wrong one.
"""

from __future__ import annotations

import logging

from app.services.stt.model_catalog import resolve_default_stt_model

logger = logging.getLogger(__name__)


def _pick_single(candidates: list[dict]) -> dict | None:
    """The one row these candidates unambiguously point at, else None.
    Enabled rows are preferred: a disabled row is a model the admin took out of
    service, so an enabled sibling is the better answer."""
    if not candidates:
        return None
    enabled = [c for c in candidates if c.get("enabled")]
    pool = enabled or candidates
    return pool[0] if len(pool) == 1 else None


async def resolve_usage_model(kind: str, engine: str, model_id: str) -> tuple[str, str]:
    """(engine, model_id) for a usage row, with blanks filled where provable.

    Never raises: any lookup failure degrades to the inputs as given, because a
    usage row with imperfect attribution beats no usage row at all.
    """
    engine = engine or ""
    model_id = model_id or ""
    if engine and model_id:
        return engine, model_id

    from app.services.model_registry.store import model_registry_store

    try:
        if not model_id and kind == "stt" and engine:
            # The STT catalog is what the provider itself consults to decide
            # which weights to load, so it's the most accurate answer available.
            catalog_model = resolve_default_stt_model(engine)
            if catalog_model:
                return engine, catalog_model

        entries = await model_registry_store.list_all()
        # Sentinel rows (model_id == "") are engine config, never a model.
        real = [e for e in entries if e["kind"] == kind and e["model_id"]]

        if not model_id and engine:
            match = _pick_single([e for e in real if e["engine"] == engine])
            if match:
                return engine, match["model_id"]

        if not model_id and not engine and kind == "llm":
            # Same entry build_responder_ex() falls back to (responder.py's
            # _active_llm_entry), so this names the model that actually ran.
            default = await model_registry_store.find_default("llm")
            if default and default["enabled"] and default["model_id"]:
                return default["engine"], default["model_id"]

        if not engine and model_id:
            match = _pick_single([e for e in real if e["model_id"] == model_id])
            if match:
                return match["engine"], model_id
    except Exception as exc:  # noqa: BLE001 - attribution must never break metering
        logger.warning("usage attribution lookup failed (%s/%s/%s): %s", kind, engine, model_id, exc)

    return engine, model_id
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_usage_attribution.py -q`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/usage/attribution.py tests/unit/test_usage_attribution.py
git commit -m "feat(usage): resolver for the (engine, model) a usage row belongs to"
```

---

### Task 2: Resolve attribution inside `record_usage`

**Files:**
- Modify: `apps/api_gateway/app/services/usage/recorder.py`
- Modify: `apps/api_gateway/app/api/routes/stt.py` (the `record_usage` call at line 123 currently hardcodes `model_id=""` and ignores the request's own `model` field)
- Test: `tests/unit/test_usage_recorder.py` (append)

**Interfaces:**
- Consumes: `resolve_usage_model` (Task 1).
- Produces: no signature change. `record_usage` resolves `(engine, model_id)` before the registry lookup, so **every** call site — present and future — records a resolved model without changing its own code.

**Why in the recorder rather than at each call site:** there are 9 `record_usage` call sites across 5 files, and the next one added would reintroduce the bug. Resolution belongs where the row is written.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_usage_recorder.py`:

```python
async def test_blank_model_is_resolved_from_the_registry_and_gets_priced():
    await init_db()
    await model_registry_store.create(
        "tts", "vieneu-rec", "vieneu-rec", "VieNeu",
        config={"provider_id": "prov-v", "price": {"unit": "1k_chars", "rate": 2.0}},
    )
    # Caller passes no model_id at all -- what /synthesize and the conversation
    # core do when a TTS profile pins no model.
    await record_usage(user_id="u1", profile_id="p1", kind="tts", engine="vieneu-rec",
                       model_id="", unit="chars", native_amount=1000)
    async with db_session() as s:
        row = (await s.execute(select(UsageEvent))).scalars().one()
    assert row.model_id == "vieneu-rec"      # no longer "" -> no more "(none)"
    assert row.provider_id == "prov-v"
    assert abs(row.cost_usd - 2.0) < 1e-12   # and now it can actually be costed


async def test_blank_engine_and_model_llm_resolves_to_the_active_default():
    await init_db()
    await model_registry_store.create(
        "llm", "openrouter-rec", "or/free-rec", "OR free", is_default=True,
    )
    await record_usage(user_id="u1", profile_id="", kind="llm", engine="", model_id="",
                       unit="tokens", native_amount=10, prompt_tokens=8, completion_tokens=2)
    async with db_session() as s:
        row = (await s.execute(select(UsageEvent))).scalars().one()
    assert (row.engine, row.model_id) == ("openrouter-rec", "or/free-rec")


async def test_unresolvable_blank_still_records_a_row():
    await init_db()
    await record_usage(user_id="u1", profile_id="", kind="tts", engine="ghost-engine",
                       model_id="", unit="chars", native_amount=5)
    async with db_session() as s:
        row = (await s.execute(select(UsageEvent))).scalars().one()
    # Nothing to resolve against -> blank is preserved, but the row must exist:
    # losing usage data is worse than an unattributed row.
    assert row.engine == "ghost-engine" and row.model_id == ""
    assert row.native_amount == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_usage_recorder.py -q`
Expected: FAIL — `assert row.model_id == "vieneu-rec"` gets `""`; the cost assertion gets `0.0`.

- [ ] **Step 3: Resolve in the recorder**

In `apps/api_gateway/app/services/usage/recorder.py`, add the import:

```python
from app.services.usage.attribution import resolve_usage_model
```

and insert as the first statement inside the existing `try:` block, before `entry = await model_registry_store.find(...)`:

```python
        # Blanks get resolved here rather than at each of the 9 call sites: a
        # blank model_id can't match the registry row that carries the price, so
        # it would silently cost $0 forever (and read as "(none)" in the UI).
        engine, model_id = await resolve_usage_model(kind, engine, model_id)
```

- [ ] **Step 4: Pass the STT route's own model through**

`routes/stt.py` accepts a `model` form field and forwards it to the provider, but hardcodes `model_id=""` when metering. Replace the `record_usage(...)` call (lines 120-127) with:

```python
        await record_usage(
            user_id=current_user_id(request) or "", profile_id="",
            kind="stt", engine=payload.engine, model_id=payload.model or "",
            unit="seconds", native_amount=data["duration"],
        )
```

and delete the now-false three-line comment above it ("model_id is intentionally `""` here ... $0 here is expected, not a bug"). When the caller omits `model`, the recorder resolves the engine's catalog default.

- [ ] **Step 5: Run the metering suites**

Run: `.venv/bin/python -m pytest tests/unit/test_usage_recorder.py tests/unit/test_routes_usage_metering.py tests/unit/test_session_usage_metering.py -q`
Expected: PASS.

Two pre-existing assertions encode the *old* contract and may need their comments updated (the assertions themselves should still hold, because both tests run against stub engines with no registry rows to resolve against):
- `tests/unit/test_routes_usage_metering.py:173` — `assert row.model_id == ""` for the echo-responder `/chat` path. Still correct: `EchoResponder` has no model and the test DB has no default LLM entry. Update the comment above it to say "no profile, no registry default, and an EchoResponder -> genuinely nothing to resolve".
- `tests/unit/test_session_usage_metering.py:248` — `assert stt.model_id == ""` after a fast-path engine switch. Still correct for a stub engine that is in neither the STT catalog nor the registry. Extend the comment: "resolution finds no catalog default and no registry row for a stub engine, so it stays blank -- the point of the assertion is that it is not the stale pin".

If either assertion *does* flip, the resolved value is the correct new expectation — assert that value and say why in the comment.

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/services/usage/recorder.py apps/api_gateway/app/api/routes/stt.py tests/unit/test_usage_recorder.py tests/unit/test_routes_usage_metering.py tests/unit/test_session_usage_metering.py
git commit -m "fix(usage): resolve blank engine/model when recording usage"
```

---

### Task 3: Attribute LLM usage to the model the responder actually used

**Files:**
- Modify: `apps/api_gateway/app/services/conversation/session.py` (`_record_llm_usage`, lines 377-399)
- Modify: `apps/api_gateway/app/api/routes/livehost.py` (`_record_llm_usage`, lines 258-273)
- Test: `tests/unit/test_session_usage_metering.py` (append)

**Interfaces:**
- Consumes: `OpenAICompatResponder.model` (`responder.py:185`) — the exact model string sent to the provider.
- Produces: no API change.

**Why the recorder's fallback isn't enough here:** the 27 `llm ('', '')` rows in production came from sessions whose profile pinned no LLM, so `build_responder_ex` fell back to the registry default. Task 1's rule 4 recovers that *today's* default, but the responder object holds the model that ran *this* turn — always the better source, and the only correct one after an admin changes the default.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_session_usage_metering.py`:

```python
async def test_llm_usage_names_the_responders_model_when_the_profile_pins_none(
    monkeypatch, tmp_path
):
    """A profile with no llm.model still runs a real model (build_responder_ex
    falls back to the registry default). The usage row must name THAT model,
    read off the responder, not blank."""
    stt_service.providers["stub-attr-stt"] = _StubSTT()
    tts_service.providers["stub-attr-tts"] = _StubTTS()

    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    monkeypatch.setattr("app.services.conversation.session.profile_store", fresh_profiles)
    # No llm.model and no llm.engine -- the case that produced ('', '') rows.
    fresh_profiles.upsert(Profile(name="attr-profile", llm=LlmConfig()))

    try:
        events: list = []

        async def emit(name, **p):
            events.append((name, p))

        async def emit_audio(pkt):
            pass

        cfg = _cfg()
        sess = ConversationSession(cfg, emit, emit_audio)
        await sess.start()
        # Stand in for whatever responder build_responder_ex returned, with the
        # model attribute a real OpenAICompatResponder carries.
        sess.responder.model = "resolved-by-responder"
        sess.responder.last_usage = {"prompt_tokens": 11, "completion_tokens": 3}
        await sess._record_llm_usage()
        await sess.close()

        rows = await _rows()
        llm = next(r for r in rows if r.kind == "llm")
        assert llm.model_id == "resolved-by-responder"
        assert llm.prompt_tokens == 11 and llm.completion_tokens == 3
    finally:
        stt_service.providers.pop("stub-attr-stt", None)
        tts_service.providers.pop("stub-attr-tts", None)


async def test_profile_pinned_model_still_wins_over_a_stale_responder_attr(
    monkeypatch, tmp_path
):
    stt_service.providers["stub-attr2-stt"] = _StubSTT()
    tts_service.providers["stub-attr2-tts"] = _StubTTS()
    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    monkeypatch.setattr("app.services.conversation.session.profile_store", fresh_profiles)
    fresh_profiles.upsert(Profile(
        name="attr2-profile",
        llm=LlmConfig(model="pinned-model", engine="pinned-engine"),
    ))
    try:
        async def emit(name, **p):
            pass

        async def emit_audio(pkt):
            pass

        sess = ConversationSession(_cfg(), emit, emit_audio)
        await sess.start()
        sess.responder.last_usage = {"prompt_tokens": 4, "completion_tokens": 1}
        await sess._record_llm_usage()
        await sess.close()
        rows = await _rows()
        llm = next(r for r in rows if r.kind == "llm")
        # The responder was built FROM the pin, so both agree; the assertion
        # guards against the responder attribute shadowing an explicit pin with
        # something unrelated.
        assert (llm.engine, llm.model_id) == ("pinned-engine", "pinned-model")
    finally:
        stt_service.providers.pop("stub-attr2-stt", None)
        tts_service.providers.pop("stub-attr2-tts", None)
```

Note: `_cfg()`, `_rows()`, `_StubSTT`, `_StubTTS`, `ProfileStore`, `Profile`, `LlmConfig` are already defined/imported at the top of this test file — reuse them, do not redefine. If `_cfg()` requires an `stt_engine`/`tts_engine` argument in this file's existing signature, pass the two stub names registered above, exactly as the neighboring tests do.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_session_usage_metering.py -q -k responders_model`
Expected: FAIL — `assert llm.model_id == "resolved-by-responder"` gets `""`.

- [ ] **Step 3: Read the responder's model in `session.py`**

In `_record_llm_usage`, replace the two resolution lines (391-392):

```python
            engine = (self.profile.llm.engine if self.profile else "") or ""
            model_id = (self.profile.llm.model if self.profile else "") or ""
```

with:

```python
            engine = (self.profile.llm.engine if self.profile else "") or ""
            # The responder holds the model actually sent to the provider -- for a
            # profile with no llm.model, build_responder_ex resolved the registry
            # default and only the responder knows which one. Falling back to the
            # profile pin keeps the explicit-pin case identical to before.
            model_id = (
                getattr(self.responder, "model", "")
                or (self.profile.llm.model if self.profile else "")
                or ""
            )
```

- [ ] **Step 4: Same in `livehost.py`**

In `livehost.py`'s `_record_llm_usage` (line 265), replace:

```python
                usage_model_id = llm_model or (profile.llm.model if profile else "") or ""
```

with:

```python
                usage_model_id = (
                    getattr(responder_obj, "model", "")
                    or llm_model
                    or (profile.llm.model if profile else "")
                    or ""
                )
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/unit/test_session_usage_metering.py tests/unit/test_routes_usage_metering.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/services/conversation/session.py apps/api_gateway/app/api/routes/livehost.py tests/unit/test_session_usage_metering.py
git commit -m "fix(usage): attribute LLM usage to the responder's actual model"
```

---

### Task 4: Backfill legacy blank `model_id` rows

**Files:**
- Create: `apps/api_gateway/app/services/usage/backfill.py`
- Modify: `apps/api_gateway/app/main.py` (register the migration next to the existing startup migrations, lines 130-155)
- Test: `tests/unit/test_usage_backfill.py`

**Interfaces:**
- Produces: `async def migrate_backfill_usage_model_ids() -> int` — returns how many rows it updated. Idempotent: a second run updates 0 (no blank rows remain for the engines it could resolve).

**Rules — provable only:**
- Only rows with `model_id == ""` and `engine != ""`.
- Candidates = registry rows with the same `kind` + `engine` and `model_id != ""` (sentinels excluded), enabled preferred, **exactly one** or skip.
- `cost_usd` is left untouched (see Global Constraints). Only `model_id` changes.
- Ambiguous and unresolvable groups are logged at INFO with their row counts, so the operator can see what was left alone rather than assuming full coverage.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_usage_backfill.py`:

```python
from sqlalchemy import select

from app.services.db.engine import db_session, init_db
from app.services.db.models import UsageEvent
from app.services.model_registry.store import model_registry_store
from app.services.usage.backfill import migrate_backfill_usage_model_ids
from app.services.usage.recorder import record_usage


async def _add_blank_row(kind, engine, native_amount=1.0):
    """A legacy row: written before attribution existed, so model_id is "".
    Inserted directly because record_usage now resolves blanks away."""
    import uuid

    async with db_session() as s:
        s.add(UsageEvent(
            id=str(uuid.uuid4()), user_id="u1", profile_id="p1", provider_id="",
            kind=kind, engine=engine, model_id="", unit="chars",
            native_amount=native_amount, cost_usd=0.0, status="ok",
        ))
        await s.commit()


async def _rows(engine):
    async with db_session() as s:
        rows = (await s.execute(select(UsageEvent))).scalars().all()
    return [r for r in rows if r.engine == engine]


async def test_backfills_when_the_engine_has_exactly_one_model():
    await init_db()
    await model_registry_store.create("tts", "vieneu-bf", "vieneu-bf", "VieNeu")
    await _add_blank_row("tts", "vieneu-bf")
    await _add_blank_row("tts", "vieneu-bf")

    assert await migrate_backfill_usage_model_ids() == 2
    assert {r.model_id for r in await _rows("vieneu-bf")} == {"vieneu-bf"}


async def test_skips_an_ambiguous_engine():
    await init_db()
    await model_registry_store.create("stt", "amb-bf", "fun-asr", "Fun")
    await model_registry_store.create("stt", "amb-bf", "qwen3-asr-flash", "Flash")
    await _add_blank_row("stt", "amb-bf")

    assert await migrate_backfill_usage_model_ids() == 0
    assert [r.model_id for r in await _rows("amb-bf")] == [""]


async def test_ignores_sentinel_rows_as_candidates():
    await init_db()
    await model_registry_store.create("stt", "sent-bf", "", "engine config")
    await model_registry_store.create("stt", "sent-bf", "real-bf", "Real")
    await _add_blank_row("stt", "sent-bf")

    assert await migrate_backfill_usage_model_ids() == 1
    assert [r.model_id for r in await _rows("sent-bf")] == ["real-bf"]


async def test_rows_with_a_blank_engine_are_left_alone():
    await init_db()
    await _add_blank_row("llm", "")
    assert await migrate_backfill_usage_model_ids() == 0
    assert [r.model_id for r in await _rows("")] == [""]


async def test_is_idempotent_and_leaves_cost_untouched():
    await init_db()
    await model_registry_store.create(
        "tts", "idem-bf", "idem-bf", "Idem",
        config={"price": {"unit": "1k_chars", "rate": 5.0}},
    )
    await _add_blank_row("tts", "idem-bf", native_amount=1000)

    assert await migrate_backfill_usage_model_ids() == 1
    assert await migrate_backfill_usage_model_ids() == 0  # nothing left to do
    row = (await _rows("idem-bf"))[0]
    assert row.model_id == "idem-bf"
    # A price entered today must not rewrite what a past request cost.
    assert row.cost_usd == 0.0


async def test_does_not_touch_rows_that_already_name_a_model():
    await init_db()
    await model_registry_store.create("tts", "keep-bf", "keep-model", "Keep")
    await record_usage(user_id="u1", profile_id="", kind="tts", engine="keep-bf",
                       model_id="keep-model", unit="chars", native_amount=10)
    assert await migrate_backfill_usage_model_ids() == 0
    assert [r.model_id for r in await _rows("keep-bf")] == ["keep-model"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_usage_backfill.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'app.services.usage.backfill'`

- [ ] **Step 3: Write the migration**

Create `apps/api_gateway/app/services/usage/backfill.py`:

```python
"""One-time, idempotent backfill of usage_events rows whose model_id is "".

Rows written before usage/attribution.py existed recorded model_id="" whenever
the caller didn't know the model (a /synthesize with no model_id, a session
whose profile pinned no LLM, ...). Those rows read as "(none)" in the Usage
dashboards and, more importantly, can never match the registry row carrying
the price.

Only PROVABLE rows are rewritten: the engine must have exactly one
non-sentinel registry model. Two candidates means either answer could be wrong,
so the row keeps its blank and the UI labels it honestly. cost_usd is never
touched -- recomputing history from today's prices would fabricate billing.

Safe on every boot: once rewritten, a row no longer matches the WHERE clause.
"""

from __future__ import annotations

import logging

from sqlalchemy import distinct, select, update

from app.services.db.engine import db_session
from app.services.db.models import UsageEvent
from app.services.model_registry.store import model_registry_store

logger = logging.getLogger(__name__)


async def migrate_backfill_usage_model_ids() -> int:
    """Number of rows updated. Never raises -- a failed backfill must not stop
    the app from booting."""
    updated = 0
    try:
        async with db_session() as s:
            groups = (
                await s.execute(
                    select(distinct(UsageEvent.kind), UsageEvent.engine)
                    .where(UsageEvent.model_id == "", UsageEvent.engine != "")
                )
            ).all()
        if not groups:
            return 0

        entries = await model_registry_store.list_all()
        for kind, engine in groups:
            candidates = [
                e for e in entries
                if e["kind"] == kind and e["engine"] == engine and e["model_id"]
            ]
            enabled = [c for c in candidates if c["enabled"]]
            pool = enabled or candidates
            if len(pool) != 1:
                logger.info(
                    "usage backfill: leaving %s/%s blank (%d candidate models)",
                    kind, engine, len(pool),
                )
                continue
            model_id = pool[0]["model_id"]
            async with db_session() as s:
                result = await s.execute(
                    update(UsageEvent)
                    .where(
                        UsageEvent.kind == kind,
                        UsageEvent.engine == engine,
                        UsageEvent.model_id == "",
                    )
                    .values(model_id=model_id)
                )
                await s.commit()
            count = result.rowcount or 0
            updated += count
            if count:
                logger.info(
                    "usage backfill: %s/%s -> model_id=%s (%d rows)",
                    kind, engine, model_id, count,
                )
    except Exception as exc:  # noqa: BLE001 - a backfill must never block boot
        logger.warning("usage model_id backfill failed: %s", exc)
    return updated
```

- [ ] **Step 4: Register it at startup**

In `apps/api_gateway/app/main.py`, next to the existing startup migrations (the import block at lines 130-140 and the call sequence at 145-155), add the import:

```python
    from app.services.usage.backfill import migrate_backfill_usage_model_ids
```

and call it **after** `migrate_drop_stale_tts_engine_shims()` (line 155) — the registry must be fully migrated before it is used as the source of truth for candidate models:

```python
    await migrate_backfill_usage_model_ids()
```

Match the surrounding style: if the neighboring migrations are imported from a shared `seed` import list, add this import as its own line rather than restructuring theirs.

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/unit/test_usage_backfill.py -q && .venv/bin/python -c "import app.main"`
Expected: PASS (6 tests), then no output from the import check.

- [ ] **Step 6: Verify against a copy of the real DB**

Never run this against `data/app.db` directly — work on a copy:

```bash
cp data/app.db /private/tmp/claude-501/-Users-lugon-code-speech-text-transformer/796fb40e-2558-4040-bf95-92b3e82332f5/scratchpad/app-backfill-check.db
.venv/bin/python - <<'PY'
import asyncio, os
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:////private/tmp/claude-501/-Users-lugon-code-speech-text-transformer/796fb40e-2558-4040-bf95-92b3e82332f5/scratchpad/app-backfill-check.db"
from app.services.db.engine import init_db
from app.services.usage.backfill import migrate_backfill_usage_model_ids

async def main():
    await init_db()
    print("updated:", await migrate_backfill_usage_model_ids())

asyncio.run(main())
PY
.venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect("/private/tmp/claude-501/-Users-lugon-code-speech-text-transformer/796fb40e-2558-4040-bf95-92b3e82332f5/scratchpad/app-backfill-check.db")
for r in c.execute("SELECT kind, engine, model_id, COUNT(*) FROM usage_events WHERE model_id = '' GROUP BY kind, engine"):
    print(r)
PY
```

Expected: `updated: 270`, and the remaining-blank listing shows exactly the three unprovable groups from the measured table above: `llm ''` (27), `stt qwencloud` (9), `stt whisper` (1). If `DATABASE_URL` is not how this app selects its DB, check `app/core/settings.py` for the actual env var and use that instead; if the env override isn't supported, skip this step and rely on the unit tests.

- [ ] **Step 7: Commit**

```bash
git add apps/api_gateway/app/services/usage/backfill.py apps/api_gateway/app/main.py tests/unit/test_usage_backfill.py
git commit -m "fix(usage): backfill provable blank model_ids in usage_events"
```

---

### Task 5: Label unattributable rows honestly in both usage views

**Files:**
- Modify: `apps/api_gateway/app/services/usage/query.py` (`summarize_for_user`, lines 75-100 — group by engine too)
- Modify: `apps/api_gateway/app/static/js/usage-me.js` (add an Engine column; `(none)` → `(not recorded)`)
- Modify: `apps/api_gateway/app/static/js/usage.js` (`(none)` → `(not recorded)`)
- Modify: `apps/api_gateway/app/static/index.html` (My Usage hint, line ~1042)
- Test: `tests/unit/test_usage_query.py` (append), `tests/unit/test_usage_routes.py` (update the `/me` shape assertion at line 89-90)

**Interfaces:**
- Produces: `/v1/usage/me` rows gain an `"engine"` key; grouping becomes `(kind, engine, model_id)`. `summarize` (admin) is unchanged.

**Why:** 37 production rows are genuinely unattributable (blank engine, or an engine with two candidate models). `(none)` reads like a bug; `(not recorded)` states what happened. Showing the engine makes even an unattributed row useful — `stt / qwencloud / (not recorded)` is actionable, `stt / (none)` is not.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_usage_query.py`:

```python
async def test_summarize_for_user_groups_by_engine_too():
    await init_db()
    from app.services.usage.recorder import record_usage

    await record_usage(user_id="u-eng", profile_id="p", kind="stt", engine="engine-a",
                       model_id="m1", unit="seconds", native_amount=10)
    await record_usage(user_id="u-eng", profile_id="p", kind="stt", engine="engine-b",
                       model_id="m1", unit="seconds", native_amount=5)

    rows = await summarize_for_user("u-eng")
    # Same kind + model, different engines -> two rows, each naming its engine.
    assert {(r["kind"], r["engine"], r["model_id"]) for r in rows} == {
        ("stt", "engine-a", "m1"),
        ("stt", "engine-b", "m1"),
    }
    assert {r["native_amount"] for r in rows} == {10.0, 5.0}
```

Update `tests/unit/test_usage_routes.py` lines 89-90 to assert the new shape:

```python
    assert body["data"][0]["kind"] == "llm"
    assert body["data"][0]["model_id"] == "qwen-max"
    assert "engine" in body["data"][0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_usage_query.py tests/unit/test_usage_routes.py -q`
Expected: FAIL — `KeyError: 'engine'`.

- [ ] **Step 3: Add engine to the per-user grouping**

In `apps/api_gateway/app/services/usage/query.py`, rewrite `summarize_for_user`'s statement and row mapping:

```python
async def summarize_for_user(user_id: str, period_key: str | None = None) -> list[dict]:
    """Same aggregation as `summarize`, scoped to one user_id and grouped by
    (kind, engine, model_id) -- the breakdown behind a user's own "my usage"
    view. Engine is part of the key because a row whose model couldn't be
    attributed (see usage/attribution.py) is still identifiable by its engine."""
    stmt = select(
        UsageEvent.kind.label("kind"),
        UsageEvent.engine.label("engine"),
        UsageEvent.model_id.label("model_id"),
        func.sum(UsageEvent.cost_usd).label("cost_usd"),
        func.sum(UsageEvent.native_amount).label("native_amount"),
        func.count().label("count"),
    ).where(UsageEvent.user_id == user_id).group_by(
        UsageEvent.kind, UsageEvent.engine, UsageEvent.model_id
    )
    if period_key:
        start, end = _period_range(period_key)
        stmt = stmt.where(UsageEvent.ts >= start, UsageEvent.ts < end)

    async with db_session() as s:
        rows = (await s.execute(stmt)).all()
    return [
        {
            "kind": row.kind,
            "engine": row.engine,
            "model_id": row.model_id,
            "cost_usd": float(row.cost_usd or 0.0),
            "native_amount": float(row.native_amount or 0.0),
            "count": int(row.count),
        }
        for row in rows
    ]
```

Also update the module docstring's `summarize_for_user` bullet (lines 6-8) to say `(kind, engine, model_id)`.

- [ ] **Step 4: Update My Usage's table**

In `apps/api_gateway/app/static/js/usage-me.js`, replace the `<thead>` row and the row template inside `_render` (lines 38-49) with:

```javascript
      <thead>
        <tr><th>Kind</th><th>Engine</th><th>Model</th><th>Cost (USD)</th><th>Native amount</th><th>Requests</th></tr>
      </thead>
      <tbody>
        ${sorted.map((r) => `
          <tr>
            <td>${escapeHtml(String(r.kind || ""))}</td>
            <td>${escapeHtml(String(r.engine || "") || "-")}</td>
            <td><code>${escapeHtml(String(r.model_id || "") || "(not recorded)")}</code></td>
            <td>${_fmtCost(r.cost_usd)}</td>
            <td>${_fmtNum(r.native_amount)}</td>
            <td>${_fmtNum(r.count)}</td>
          </tr>`).join("")}
      </tbody>
```

and bump the footer's `colspan` from 2 to 3 (line 53) so Total still spans the label columns:

```javascript
          <td colspan="3"><strong>Total</strong></td>
```

- [ ] **Step 5: Update the admin Usage table's placeholder**

In `apps/api_gateway/app/static/js/usage.js` line 65, change the `(none)` fallback:

```javascript
            <td><code>${escapeHtml(String(r.key || "") || "(not recorded)")}</code></td>
```

- [ ] **Step 6: Explain the label once, in the My Usage hint**

In `index.html` (the `#section-my-usage` hint, line ~1042), append one sentence to the existing hint text:

```
Rows marked "(not recorded)" are older requests logged before per-model attribution; the engine is still shown.
```

- [ ] **Step 7: Verify**

```bash
.venv/bin/python -m pytest tests/unit/test_usage_query.py tests/unit/test_usage_routes.py -q
node --check apps/api_gateway/app/static/js/usage-me.js
node --check apps/api_gateway/app/static/js/usage.js
grep -nE '[‘’“”]' apps/api_gateway/app/static/js/usage-me.js apps/api_gateway/app/static/js/usage.js apps/api_gateway/app/static/index.html || echo "no smart quotes"
```
Expected: tests PASS, `node --check` silent, "no smart quotes". Then Read `usage-me.js` back to confirm the table has 6 columns and the footer colspan is 3.

Also check whether the React client renders `/v1/usage/me`:

```bash
grep -rn "usage/me" lugo-web-client/src 2>/dev/null || echo "React client does not consume /v1/usage/me"
```
If it does, the added `engine` key is additive (no breakage), but note in the commit message that the React screen still shows only kind/model.

- [ ] **Step 8: Commit**

```bash
git add apps/api_gateway/app/services/usage/query.py apps/api_gateway/app/static/js/usage-me.js apps/api_gateway/app/static/js/usage.js apps/api_gateway/app/static/index.html tests/unit/test_usage_query.py tests/unit/test_usage_routes.py
git commit -m "feat(usage): show engine in My Usage, label unattributed rows"
```

---

## Phase 2 — Pricing

### Task 6: Price schema validator

**Files:**
- Create: `apps/api_gateway/app/services/usage/price_schema.py`
- Test: `tests/unit/test_usage_price_schema.py`

**Interfaces:**
- Consumes: `compute_cost` from `app.services.usage.pricing` (only in a test, to prove the two modules agree).
- Produces:
  - `PRICE_UNIT_BY_KIND: dict[str, str]` — `{"llm": "1M_tokens", "embed": "1M_tokens", "stt": "minute", "tts": "1k_chars"}`
  - `validate_price(kind: str, price) -> dict | None` — normalized dict, or `None` meaning "no price / clear it". Raises `ValueError` with an admin-readable message.
  - `apply_price_to_config(kind: str, config: dict, price) -> dict` — a **copy** of `config` with the validated price merged in (or `"price"` removed when `price` is `None`/`{}`).

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_usage_price_schema.py`:

```python
import pytest

from app.services.usage.price_schema import (
    PRICE_UNIT_BY_KIND,
    apply_price_to_config,
    validate_price,
)
from app.services.usage.pricing import compute_cost


def test_llm_price_normalized_with_unit_filled_from_kind():
    assert validate_price("llm", {"in": 0.15, "out": 0.6}) == {
        "unit": "1M_tokens", "in": 0.15, "out": 0.6,
    }


def test_missing_rate_key_defaults_to_zero():
    # An embedding model priced input-only is the normal case.
    assert validate_price("embed", {"in": 0.02}) == {"unit": "1M_tokens", "in": 0.02, "out": 0.0}


def test_stt_and_tts_units():
    assert validate_price("stt", {"rate": 0.0032}) == {"unit": "minute", "rate": 0.0032}
    assert validate_price("tts", {"rate": 0.015}) == {"unit": "1k_chars", "rate": 0.015}


def test_explicit_matching_unit_is_accepted():
    assert validate_price("tts", {"unit": "1k_chars", "rate": 1.0})["rate"] == 1.0


def test_wrong_unit_for_kind_is_rejected():
    with pytest.raises(ValueError, match="must be '1k_chars'"):
        validate_price("tts", {"unit": "1M_tokens", "in": 1.0})


def test_unknown_field_is_rejected():
    # The whole point: "input" instead of "in" used to cost $0 forever, silently.
    with pytest.raises(ValueError, match="unknown price field"):
        validate_price("llm", {"input": 0.15})


def test_no_rate_key_at_all_is_rejected():
    with pytest.raises(ValueError, match="at least one of"):
        validate_price("llm", {"unit": "1M_tokens"})


def test_bool_and_negative_and_nonnumeric_rates_are_rejected():
    with pytest.raises(ValueError, match="must be a number"):
        validate_price("stt", {"rate": True})
    with pytest.raises(ValueError, match="must be a number"):
        validate_price("stt", {"rate": "0.01"})
    with pytest.raises(ValueError, match=">= 0"):
        validate_price("stt", {"rate": -1.0})


def test_empty_or_none_means_no_price():
    assert validate_price("llm", None) is None
    assert validate_price("llm", {}) is None


def test_non_dict_price_is_rejected():
    with pytest.raises(ValueError, match="must be an object"):
        validate_price("llm", [0.15, 0.6])


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError, match="unknown kind"):
        validate_price("vision", {"in": 1.0})


def test_every_kind_has_a_unit_compute_cost_understands():
    # Guards the contract between this module and pricing.compute_cost: a unit
    # this module blesses but compute_cost ignores would be a silent $0.
    for kind, unit in PRICE_UNIT_BY_KIND.items():
        price = validate_price(kind, {"in": 1.0} if unit == "1M_tokens" else {"rate": 60.0})
        cost = compute_cost(price, 1_000_000, 0, 60.0)
        assert cost > 0, f"{kind}/{unit} costed nothing"


def test_apply_price_preserves_other_config_keys():
    config = {"provider_id": "prov-1", "device": "cpu"}
    merged = apply_price_to_config("llm", config, {"in": 0.15})
    assert merged == {"provider_id": "prov-1", "device": "cpu",
                      "price": {"unit": "1M_tokens", "in": 0.15, "out": 0.0}}
    assert config == {"provider_id": "prov-1", "device": "cpu"}  # not mutated


def test_apply_price_none_clears_only_the_price_key():
    config = {"provider_id": "prov-1", "price": {"unit": "minute", "rate": 1.0}}
    assert apply_price_to_config("stt", config, None) == {"provider_id": "prov-1"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_usage_price_schema.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'app.services.usage.price_schema'`

- [ ] **Step 3: Write the implementation**

Create `apps/api_gateway/app/services/usage/price_schema.py`:

```python
"""Write-time validation for a Model Registry entry's config["price"].

compute_cost (usage/pricing.py) resolves an unrecognized price to $0.0 --
the right runtime fallback, but a terrible sole feedback channel for an
admin who typed "input" instead of "in". This module is the write-time gate:
every path that stores a price runs it, so a bad shape is a 400 at save time
rather than a silently free month of billing.

The unit is derived from the kind and never free-typed:
  llm/embed -> "1M_tokens", USD per 1M tokens, keys "in"/"out"
  stt       -> "minute",    USD per minute of audio, key "rate"
  tts       -> "1k_chars",  USD per 1000 characters, key "rate"
"""

from __future__ import annotations

PRICE_UNIT_BY_KIND = {
    "llm": "1M_tokens",
    "embed": "1M_tokens",
    "stt": "minute",
    "tts": "1k_chars",
}

# Rate keys each unit accepts, named exactly as compute_cost reads them.
_RATE_KEYS = {"1M_tokens": ("in", "out"), "minute": ("rate",), "1k_chars": ("rate",)}


def _as_rate(value, key: str) -> float:
    # bool before the numeric check -- bool is an int subclass, so without this
    # price={"rate": True} would quietly become $1.00 per minute.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"price.{key} must be a number, got {value!r}")
    rate = float(value)
    if rate < 0:
        raise ValueError(f"price.{key} must be >= 0, got {rate}")
    return rate


def validate_price(kind: str, price) -> dict | None:
    """Normalized price for `kind`, or None meaning "no price / clear it".

    Raises ValueError (message is surfaced verbatim to the admin) otherwise.
    """
    if kind not in PRICE_UNIT_BY_KIND:
        raise ValueError(
            f"unknown kind '{kind}' for pricing "
            f"(expected one of {sorted(PRICE_UNIT_BY_KIND)})"
        )
    if price is None or price == {}:
        return None
    if not isinstance(price, dict):
        raise ValueError(f"price must be an object, got {type(price).__name__}")

    unit = PRICE_UNIT_BY_KIND[kind]
    given_unit = price.get("unit")
    if given_unit is not None and given_unit != unit:
        raise ValueError(f"price.unit for kind '{kind}' must be '{unit}', got '{given_unit}'")

    rate_keys = _RATE_KEYS[unit]
    unknown = sorted(set(price) - {"unit"} - set(rate_keys))
    if unknown:
        raise ValueError(
            f"unknown price field(s) {unknown} for unit '{unit}' (expected {list(rate_keys)})"
        )
    if not any(key in price for key in rate_keys):
        raise ValueError(f"price for unit '{unit}' needs at least one of {list(rate_keys)}")

    normalized = {"unit": unit}
    for key in rate_keys:
        normalized[key] = _as_rate(price[key], key) if key in price else 0.0
    return normalized


def apply_price_to_config(kind: str, config: dict, price) -> dict:
    """A copy of `config` with a validated price merged in (or the "price" key
    removed when price is None/{}). Merges rather than replaces so provider_id
    and the engine's own config keys survive a pricing edit."""
    validated = validate_price(kind, price)
    merged = dict(config or {})
    if validated is None:
        merged.pop("price", None)
    else:
        merged["price"] = validated
    return merged
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_usage_price_schema.py -q`
Expected: PASS (14 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/usage/price_schema.py tests/unit/test_usage_price_schema.py
git commit -m "feat(usage): write-time price validation (price_schema)"
```

---

### Task 7: Bulk price API + validation on registry writes

**Files:**
- Modify: `apps/api_gateway/app/api/routes/model_registry.py` (add import + 2 routes after `get_config_schema` at lines 198-203; validate in `create_entry` ~line 258 and `update_entry` ~line 336)
- Test: `tests/unit/test_model_registry_prices_routes.py`

**Interfaces:**
- Consumes: `PRICE_UNIT_BY_KIND`, `apply_price_to_config` (Task 6).
- Produces:
  - `GET /v1/model_registry/prices` → `{"success": true, "data": [{"id", "kind", "engine", "model_id", "label", "provider_id", "unit", "price"}]}` — one row per registry entry, `price` is `null` when unpriced.
  - `PATCH /v1/model_registry/prices` with body `{"prices": [{"id": str, "price": dict | null}]}` → `{"success": true, "data": {"updated": int}}`; 400 on any invalid price (nothing written), 404 on an unknown id.

**CRITICAL — route ordering:** declare both new routes immediately after `get_config_schema` (line 203). FastAPI matches in declaration order, so `PATCH /prices` declared *after* `PATCH /{entry_id}` would be swallowed with `entry_id == "prices"`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_model_registry_prices_routes.py`:

```python
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.db.engine import init_db
from app.services.model_registry.store import model_registry_store


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _login_admin(client, username="pricadm"):
    from app.services.auth.users import user_store
    client.post("/api/auth/signup", json={"username": username, "password": "pw"})
    user = asyncio.run(user_store.get_by_username(username))
    asyncio.run(user_store.set_fields(user.id, role="admin"))
    client.post("/api/auth/login", json={"username": username, "password": "pw"})


def _seed_entry(kind="llm", engine="openai", model_id="gpt-4o-mini", config=None):
    asyncio.run(init_db())
    return asyncio.run(
        model_registry_store.create(
            kind, engine, model_id, f"{engine}/{model_id}",
            config=config if config is not None else {"provider_id": "prov-1"},
        )
    )


def test_regular_user_cannot_reach_prices(client, _with_password):
    client.post("/api/auth/signup", json={"username": "bobprice", "password": "pw"})
    client.post("/api/auth/login", json={"username": "bobprice", "password": "pw"})
    assert client.get("/v1/model_registry/prices").status_code == 403


def test_list_prices_includes_unpriced_rows_and_the_kinds_unit(client, _with_password):
    _login_admin(client)
    entry = _seed_entry()
    rows = client.get("/v1/model_registry/prices").json()["data"]
    row = next(r for r in rows if r["id"] == entry["id"])
    assert row["price"] is None
    assert row["unit"] == "1M_tokens"
    assert row["provider_id"] == "prov-1"
    assert row["kind"] == "llm" and row["model_id"] == "gpt-4o-mini"


def test_bulk_patch_sets_price_and_preserves_provider_id(client, _with_password):
    _login_admin(client)
    entry = _seed_entry(model_id="gpt-4o-price")
    resp = client.patch("/v1/model_registry/prices", json={
        "prices": [{"id": entry["id"], "price": {"in": 0.15, "out": 0.6}}],
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["updated"] == 1
    stored = asyncio.run(model_registry_store.get(entry["id"]))
    assert stored["config"] == {
        "provider_id": "prov-1",
        "price": {"unit": "1M_tokens", "in": 0.15, "out": 0.6},
    }


def test_bulk_patch_null_price_clears_it(client, _with_password):
    _login_admin(client)
    entry = _seed_entry(
        model_id="gpt-4o-clear",
        config={"provider_id": "prov-1", "price": {"unit": "1M_tokens", "in": 1.0, "out": 2.0}},
    )
    resp = client.patch("/v1/model_registry/prices",
                        json={"prices": [{"id": entry["id"], "price": None}]})
    assert resp.status_code == 200, resp.text
    assert asyncio.run(model_registry_store.get(entry["id"]))["config"] == {"provider_id": "prov-1"}


def test_bulk_patch_rejects_all_or_nothing_on_a_bad_price(client, _with_password):
    _login_admin(client)
    good = _seed_entry(model_id="gpt-4o-good")
    bad = _seed_entry(model_id="gpt-4o-bad")
    resp = client.patch("/v1/model_registry/prices", json={"prices": [
        {"id": good["id"], "price": {"in": 0.15}},
        {"id": bad["id"], "price": {"input": 0.15}},
    ]})
    assert resp.status_code == 400
    assert "unknown price field" in resp.json()["detail"]
    # The valid row must NOT have been written -- a half-applied price table is
    # worse than a rejected one, the admin can't tell which rows landed.
    assert "price" not in asyncio.run(model_registry_store.get(good["id"]))["config"]


def test_bulk_patch_unknown_id_is_404(client, _with_password):
    _login_admin(client)
    resp = client.patch("/v1/model_registry/prices",
                        json={"prices": [{"id": "nope", "price": {"in": 1.0}}]})
    assert resp.status_code == 404


def test_create_rejects_a_bad_price_before_any_network_call(client, _with_password):
    _login_admin(client)
    # No httpx mocking on purpose: validation runs before the add-time test
    # call, so a bad price must 400 without the route ever reaching out.
    resp = client.post("/v1/model_registry", json={
        "kind": "llm", "engine": "openai", "model_id": "m", "label": "M",
        "base_url": "http://127.0.0.1:9/v1",
        "config": {"price": {"unit": "minute", "rate": 1.0}},
    })
    assert resp.status_code == 400
    assert "must be '1M_tokens'" in resp.json()["detail"]


def test_patch_entry_config_validates_price(client, _with_password):
    _login_admin(client)
    entry = _seed_entry(model_id="gpt-4o-patch")
    resp = client.patch(f"/v1/model_registry/{entry['id']}",
                        json={"config": {"price": {"in": "cheap"}}})
    assert resp.status_code == 400
    assert "must be a number" in resp.json()["detail"]

    ok = client.patch(f"/v1/model_registry/{entry['id']}",
                      json={"config": {"provider_id": "prov-1", "price": {"in": 0.2}}})
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["config"]["price"] == {"unit": "1M_tokens", "in": 0.2, "out": 0.0}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_model_registry_prices_routes.py -q`
Expected: FAIL — `/v1/model_registry/prices` 404s or 422s, and the create/patch validation tests don't 400.

- [ ] **Step 3: Add the import and the two routes**

In `apps/api_gateway/app/api/routes/model_registry.py`, add to the imports (next to the other `app.services.*` imports, around line 15):

```python
from app.services.usage.price_schema import PRICE_UNIT_BY_KIND, apply_price_to_config
```

Insert immediately after `get_config_schema` (after line 203, before `_VALID_KINDS`):

```python
class PriceItem(BaseModel):
    id: str
    price: dict | None = None


class BulkPriceRequest(BaseModel):
    prices: list[PriceItem]


# NOTE: /prices must stay ABOVE the "/{entry_id}" routes -- FastAPI matches in
# declaration order, so a later PATCH /{entry_id} would swallow this as
# entry_id="prices".
@router.get("/prices")
async def list_prices() -> dict:
    """Every registry entry with its pricing, for the admin Pricing tab.
    Unpriced entries are included with price=null -- "which models are still
    uncosted" is the main question this table answers."""
    data = []
    for entry in await model_registry_store.list_all():
        config = entry.get("config") or {}
        data.append({
            "id": entry["id"],
            "kind": entry["kind"],
            "engine": entry["engine"],
            "model_id": entry["model_id"],
            "label": entry["label"],
            "provider_id": config.get("provider_id", ""),
            "unit": PRICE_UNIT_BY_KIND.get(entry["kind"], ""),
            "price": config.get("price"),
        })
    return {"success": True, "data": data}


@router.patch("/prices")
async def update_prices(payload: BulkPriceRequest) -> dict:
    """Bulk price save. Validates EVERY item before writing ANY of them: a
    half-applied price table leaves the admin unable to tell which rows landed."""
    entries = {e["id"]: e for e in await model_registry_store.list_all()}
    planned: list[tuple[str, dict]] = []
    for item in payload.prices:
        entry = entries.get(item.id)
        if entry is None:
            raise HTTPException(
                status_code=404, detail=f"model registry entry '{item.id}' not found"
            )
        try:
            planned.append((
                item.id,
                apply_price_to_config(entry["kind"], entry.get("config") or {}, item.price),
            ))
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"{entry['label'] or item.id}: {exc}"
            ) from exc
    for entry_id, config in planned:
        await model_registry_store.set_fields(entry_id, config=config)
    return {"success": True, "data": {"updated": len(planned)}}
```

- [ ] **Step 4: Validate price on the create path**

In `create_entry`, insert as the **first statement of the function body** (before `_validate_known_engine`, so a bad price never triggers the live test call):

```python
    # Validate/normalize the price before anything else: the add-time test call
    # is a real network round-trip, and a typo'd price shouldn't cost one.
    try:
        payload.config = apply_price_to_config(
            payload.kind, payload.config, (payload.config or {}).get("price")
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
```

- [ ] **Step 5: Validate price on the patch path**

In `update_entry`, insert right after the `fields = {...}` line (currently line 337) and before the `was_enabled` block:

```python
    if "config" in fields and "price" in (fields["config"] or {}):
        # The kind isn't in the payload -- it comes from the stored row, which is
        # also what tells us which unit this price must use.
        existing = await model_registry_store.get(entry_id)
        if existing is None:
            raise HTTPException(
                status_code=404, detail=f"model registry entry '{entry_id}' not found"
            )
        try:
            fields["config"] = apply_price_to_config(
                existing["kind"], fields["config"], fields["config"]["price"]
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest tests/unit/test_model_registry_prices_routes.py -q && .venv/bin/python -m pytest tests/unit -q -k model_registry`
Expected: PASS both. The second command is the regression check on every existing registry test.

- [ ] **Step 7: Commit**

```bash
git add apps/api_gateway/app/api/routes/model_registry.py tests/unit/test_model_registry_prices_routes.py
git commit -m "feat(usage): bulk price API + price validation on registry writes"
```

---

### Task 8: `embed` as a first-class registry kind

**Files:**
- Modify: `apps/api_gateway/app/api/routes/model_registry.py` (`_location` lines 68-90, `_VALID_KINDS` line 206, `create_entry`'s kind branches lines 276-313)
- Test: `tests/unit/test_model_registry_embed_kind.py`

**Interfaces:**
- Consumes: `embed_texts(texts, base_url, api_key, model)` from `app.services.memory.embedder`.
- Produces: registry entries with `kind="embed"`. Tasks 10/11's `record_usage(kind="embed", ...)` calls resolve their provider_id + price through exactly these rows.

**Why:** `record_usage` prices a row via `find(kind, engine, model_id)`. Without an `embed` kind there is nowhere to put an embedding model's price, so embedding usage could never be costed.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_model_registry_embed_kind.py`:

```python
import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.model_registry.availability import is_artifact_installed
from app.services.model_registry.store import model_registry_store


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _login_admin(client, username="embadm"):
    from app.services.auth.users import user_store
    client.post("/api/auth/signup", json={"username": username, "password": "pw"})
    user = asyncio.run(user_store.get_by_username(username))
    asyncio.run(user_store.set_fields(user.id, role="admin"))
    client.post("/api/auth/login", json={"username": username, "password": "pw"})


@pytest.fixture
def _fake_embeddings(monkeypatch):
    """Stand in for the provider's /embeddings endpoint during the add-time test call."""
    calls = {}

    async def fake_post(self, url, headers=None, json=None):
        calls["url"] = url
        calls["json"] = json

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"data": [{"embedding": [0.1, 0.2]}], "usage": {"prompt_tokens": 3}}

        return R()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return calls


def test_create_embed_entry_runs_a_live_embed_test_then_persists(
    client, _with_password, _fake_embeddings
):
    _login_admin(client)
    resp = client.post("/v1/model_registry", json={
        "kind": "embed", "engine": "openai", "model_id": "text-embedding-3-small",
        "label": "OpenAI embed small", "base_url": "http://llm.local/v1", "api_key": "k",
        "config": {"price": {"in": 0.02}},
    })
    assert resp.status_code == 200, resp.text
    assert _fake_embeddings["url"] == "http://llm.local/v1/embeddings"
    created = resp.json()["data"]
    assert created["kind"] == "embed"
    assert created["config"]["price"] == {"unit": "1M_tokens", "in": 0.02, "out": 0.0}


def test_embed_entries_are_service_and_need_a_base_url(client, _with_password, _fake_embeddings):
    _login_admin(client)
    client.post("/v1/model_registry", json={
        "kind": "embed", "engine": "openai", "model_id": "text-embedding-3-large",
        "label": "OpenAI embed large", "base_url": "http://llm.local/v1",
    })
    rows = client.get("/v1/model_registry").json()["data"]
    row = next(r for r in rows if r["model_id"] == "text-embedding-3-large")
    assert row["location"] == "service"
    assert row["requires_base_url"] is True


def test_options_accepts_the_embed_kind(client, _with_password):
    _login_admin(client)
    assert client.get("/v1/model_registry/options?kind=embed").status_code == 200
    assert client.get("/v1/model_registry/options?kind=bogus").status_code == 400


def test_embed_has_no_artifact_install_gate():
    # There is no local artifact for a remote embedding model; None means
    # "not applicable" and must not block enabling the row.
    assert is_artifact_installed("embed", "openai", "text-embedding-3-small") is None


def test_recorder_prices_embed_usage_through_an_embed_registry_row():
    from sqlalchemy import select

    from app.services.db.engine import db_session, init_db
    from app.services.db.models import UsageEvent
    from app.services.usage.recorder import record_usage

    async def _run():
        await init_db()
        await model_registry_store.create(
            "embed", "openai", "text-embedding-3-priced", "priced embed",
            config={"provider_id": "prov-e", "price": {"unit": "1M_tokens", "in": 0.02, "out": 0.0}},
        )
        await record_usage(user_id="u1", profile_id="p1", kind="embed", engine="openai",
                           model_id="text-embedding-3-priced", unit="tokens",
                           native_amount=1_000_000, prompt_tokens=1_000_000)
        async with db_session() as s:
            rows = (await s.execute(select(UsageEvent))).scalars().all()
        return [r for r in rows if r.model_id == "text-embedding-3-priced"]

    rows = asyncio.run(_run())
    assert len(rows) == 1
    assert rows[0].provider_id == "prov-e"
    assert abs(rows[0].cost_usd - 0.02) < 1e-12
    assert rows[0].kind == "embed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_model_registry_embed_kind.py -q`
Expected: FAIL — create returns 400 `unknown kind 'embed'`; `options?kind=embed` returns 400.

- [ ] **Step 3: Accept the kind**

In `apps/api_gateway/app/api/routes/model_registry.py`, change line 206:

```python
_VALID_KINDS = {"stt", "tts", "llm", "embed"}
```

- [ ] **Step 4: Classify embed entries as remote services**

In `_location`, change the first condition (lines 81-82) from `kind == "llm"` to:

```python
    if (
        kind in ("llm", "embed")
        or engine in _SERVICE_STT_ENGINES
```

Extend that function's docstring bullet: after "and every `kind="llm"` entry", add "and every `kind="embed"` entry (an OpenAI-compatible `/embeddings` host)". `_requires_base_url` needs no change — it derives from `_location`, and `embed` is not in `_FIXED_ENDPOINT_STT_ENGINES`, so it correctly returns True.

- [ ] **Step 5: Add the add-time test call**

In `create_entry`, add a branch after the `elif payload.kind == "llm":` block (after line 311, before the `else: raise ... unknown kind`):

```python
        elif payload.kind == "embed":
            # Same "prove it works before we store it" contract as the other
            # kinds: one tiny embed call validates endpoint + key + model id.
            from app.services.memory.embedder import embed_texts

            await embed_texts([payload.sample_text], eff_base_url, eff_api_key, payload.model_id)
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest tests/unit/test_model_registry_embed_kind.py tests/unit/test_model_registry_prices_routes.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add apps/api_gateway/app/api/routes/model_registry.py tests/unit/test_model_registry_embed_kind.py
git commit -m "feat(registry): embed as a first-class kind (priceable embedding models)"
```

---

### Task 9: Pricing tab (admin static UI)

**Files:**
- Create: `apps/api_gateway/app/static/js/pricing.js`
- Modify: `apps/api_gateway/app/static/index.html` (nav item after the Usage `<li>` at lines 123-128; new `<div class="section" id="section-pricing">` after `#section-usage`, which ends at line 987; add `<option value="embed">embed</option>` to both kind selects at lines 828-833 and 861-865; update the admin Usage hint at line 965)
- Modify: `apps/api_gateway/app/static/js/sidebar-nav.js` (import + `activateSection` dispatch)
- Modify: `apps/api_gateway/app/static/js/model-registry.js` (treat `embed` like `llm` in the add form: lines 51, 92, 620, 649)

**Interfaces:**
- Consumes: `GET /v1/model_registry/prices`, `PATCH /v1/model_registry/prices` (Task 7); `el`, `print`, `escapeHtml` from `./helpers.js`.
- Produces: `loadPricing()` exported from `pricing.js`, called by `sidebar-nav.js` when the `pricing` section activates.

**Reminder:** ASCII quotes only. After editing, Read each file back and confirm no `’ “ ”` crept in.

- [ ] **Step 1: Add the nav item**

In `index.html`, insert after the Usage `<li>` (after line 128, before the Quotas `<li>`):

```html
            <li class="admin-only">
              <button class="nav-item" data-section="pricing">
                <span class="nav-icon">&#128176;</span>
                <span class="nav-label">Pricing</span>
              </button>
            </li>
```

- [ ] **Step 2: Add the section markup**

In `index.html`, insert immediately after the closing `</div>` of `#section-usage` (after line 987, before the QUOTAS comment):

```html
          <!-- ============================== PRICING ============================== -->
          <div class="section" id="section-pricing">
            <section class="card">
              <div class="card-head">
                <h2>Pricing</h2>
                <button id="pricing-refresh" class="ghost mini">Refresh</button>
              </div>
              <p class="hint">Per-model prices. A model with no price still records usage, but at $0 cost, and $-based quotas never see it. Units are fixed per kind: llm/embed = USD per 1M tokens (in/out), stt = USD per minute of audio, tts = USD per 1000 characters.</p>
              <label class="inline">
                <input id="pricing-only-unpriced" type="checkbox" />
                Show only models without a price
              </label>
              <div id="pricing-list" class="model-list">
                <p class="hint">Loading&#8230;</p>
              </div>
              <div class="actions end">
                <button id="pricing-save-btn">Save prices</button>
              </div>
              <p id="pricing-status" class="meta"></p>
            </section>
          </div>
```

- [ ] **Step 3: Update the admin Usage hint and both kind dropdowns**

In `index.html`:
- Line 965 ends with "Cost is $0 for models without a configured price." Append " Set prices in the Pricing tab."
- Add `<option value="embed">embed</option>` after the `llm` option in **both** `#registry-filter-kind` (line 832) and `#registry-add-kind` (line 864).

- [ ] **Step 4: Write `pricing.js`**

Create `apps/api_gateway/app/static/js/pricing.js`:

```javascript
import { el, print, escapeHtml } from "./helpers.js";

// Rate inputs per unit, in the order the server's price_schema normalizes them.
const RATE_KEYS = {
  "1M_tokens": [
    { key: "in", label: "in / 1M" },
    { key: "out", label: "out / 1M" },
  ],
  minute: [{ key: "rate", label: "$ / minute" }],
  "1k_chars": [{ key: "rate", label: "$ / 1k chars" }],
};

let pricingRows = [];

export async function loadPricing() {
  const host = el("pricing-list");
  if (!host) return;
  try {
    const resp = await fetch("/v1/model_registry/prices");
    const body = await resp.json();
    if (!resp.ok) {
      print(el("pricing-status"), body.detail || "Failed to load prices", true);
      return;
    }
    pricingRows = body.data || [];
    _render();
    if (el("pricing-status")) el("pricing-status").textContent = "";
  } catch (error) {
    print(el("pricing-status"), String(error), true);
  }
}

function _render() {
  const host = el("pricing-list");
  if (!host) return;
  const onlyUnpriced = !!el("pricing-only-unpriced")?.checked;
  const rows = pricingRows
    .filter((r) => RATE_KEYS[r.unit]) // a kind with no priceable unit can't be edited here
    .filter((r) => !onlyUnpriced || !r.price)
    .sort((a, b) => a.kind.localeCompare(b.kind) || a.engine.localeCompare(b.engine) || a.model_id.localeCompare(b.model_id));
  if (!rows.length) {
    host.innerHTML = `<p class="hint">${onlyUnpriced ? "Every model has a price." : "No registry entries yet."}</p>`;
    return;
  }
  host.innerHTML = `
    <table class="data-table">
      <thead>
        <tr>
          <th>Kind</th>
          <th>Engine</th>
          <th>Model</th>
          <th>Unit</th>
          <th>Price (USD)</th>
        </tr>
      </thead>
      <tbody>
        ${rows.map(_renderRow).join("")}
      </tbody>
    </table>`;
}

function _renderRow(row) {
  const inputs = RATE_KEYS[row.unit]
    .map((field) => {
      const value = row.price && row.price[field.key] != null ? String(row.price[field.key]) : "";
      return `
        <label class="price-field">
          <span>${escapeHtml(field.label)}</span>
          <input type="number" class="mini" step="0.000001" min="0"
                 data-price-id="${escapeHtml(row.id)}" data-price-key="${escapeHtml(field.key)}"
                 value="${escapeHtml(value)}" placeholder="0" />
        </label>`;
    })
    .join("");
  return `
    <tr class="${row.price ? "" : "dim"}">
      <td><code>${escapeHtml(row.kind)}</code></td>
      <td>${escapeHtml(row.engine)}</td>
      <td><code>${escapeHtml(row.model_id || "(engine config)")}</code></td>
      <td>${escapeHtml(row.unit)}</td>
      <td>${inputs}</td>
    </tr>`;
}

// One item per row that has at least one non-blank rate input; a row whose
// inputs are all blank is sent as price:null so clearing a price is possible.
function _collect() {
  const byId = new Map();
  document.querySelectorAll("[data-price-id]").forEach((input) => {
    const id = input.getAttribute("data-price-id");
    const key = input.getAttribute("data-price-key");
    const raw = input.value.trim();
    if (!byId.has(id)) byId.set(id, {});
    if (raw !== "") byId.get(id)[key] = Number(raw);
  });
  const items = [];
  for (const [id, price] of byId.entries()) {
    const row = pricingRows.find((r) => r.id === id);
    const hasValues = Object.keys(price).length > 0;
    const hadPrice = !!(row && row.price);
    if (!hasValues && !hadPrice) continue; // nothing to do for an untouched unpriced row
    items.push({ id, price: hasValues ? price : null });
  }
  return items;
}

export async function savePricing() {
  const status = el("pricing-status");
  const items = _collect();
  if (!items.length) {
    print(status, "No prices to save", true);
    return;
  }
  const invalid = items.find((i) => i.price && Object.values(i.price).some((v) => Number.isNaN(v)));
  if (invalid) {
    print(status, "A price field is not a number", true);
    return;
  }
  status.textContent = "Saving…";
  try {
    const resp = await fetch("/v1/model_registry/prices", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prices: items }),
    });
    const body = await resp.json();
    if (!resp.ok) {
      // The server validates all-or-nothing, so nothing was written.
      print(status, body.detail || "Save failed (no prices were changed)", true);
      return;
    }
    status.textContent = `Saved ${body.data.updated} price row(s)`;
    await loadPricing();
  } catch (error) {
    print(status, String(error), true);
  }
}

if (el("pricing-refresh")) el("pricing-refresh").addEventListener("click", loadPricing);
if (el("pricing-save-btn")) el("pricing-save-btn").addEventListener("click", savePricing);
if (el("pricing-only-unpriced")) el("pricing-only-unpriced").addEventListener("change", _render);
```

- [ ] **Step 5: Wire the tab into the sidebar**

In `apps/api_gateway/app/static/js/sidebar-nav.js`, add next to the other imports:

```javascript
import { loadPricing } from "./pricing.js";
```

and inside `activateSection`, after the `if (section === "usage") loadUsage();` line:

```javascript
  if (section === "pricing") loadPricing();
```

- [ ] **Step 6: Let the add-entry form handle `embed` like `llm`**

In `apps/api_gateway/app/static/js/model-registry.js`, four edits:

Line 51 — hide the engine dropdown for embed (engine is the provider name, not a local runtime):
```javascript
  if (kind === "llm" || kind === "embed" || hasProvider) { if (wrap) wrap.classList.add("hidden"); void _loadModelChoices(announce); return; }
```

Line 92 in `_effectiveEngine` — derive the engine from the provider name:
```javascript
  if (kind === "llm" || kind === "embed") {
```

Line 620 in `_updateKindFields` — embed needs the Base URL + paired API Key fields, same as llm/stt:
```javascript
  const isLlmOrStt = kind === "llm" || kind === "embed" || kind === "stt";
```

Line 649 in `createModelRegistryEntry` — submit base_url/api_key from the paired fields:
```javascript
  } else if (kind === "llm" || kind === "embed" || kind === "stt") {
```

- [ ] **Step 7: Verify the JS parses and the ids line up**

```bash
node --check apps/api_gateway/app/static/js/pricing.js
node --check apps/api_gateway/app/static/js/sidebar-nav.js
node --check apps/api_gateway/app/static/js/model-registry.js
grep -o 'id="pricing[^"]*"' apps/api_gateway/app/static/index.html | sort -u
grep -o 'el("pricing[^"]*")' apps/api_gateway/app/static/js/pricing.js | sort -u
grep -nE '[‘’“”]' apps/api_gateway/app/static/js/pricing.js apps/api_gateway/app/static/index.html || echo "no smart quotes"
```
Expected: `node --check` silent for all three; every `el("pricing-...")` id in the JS appears in the HTML list (`pricing-list`, `pricing-status`, `pricing-refresh`, `pricing-save-btn`, `pricing-only-unpriced`); the smart-quote grep prints "no smart quotes".

Then Read `apps/api_gateway/app/static/js/pricing.js` and the edited `index.html` block to confirm the content matches this plan (`node --check` cannot catch quote corruption inside string literals).

- [ ] **Step 8: Verify the app still imports**

```bash
.venv/bin/python -c "import app.main"
```
Expected: no output.

- [ ] **Step 9: Commit**

```bash
git add apps/api_gateway/app/static/js/pricing.js apps/api_gateway/app/static/js/sidebar-nav.js apps/api_gateway/app/static/js/model-registry.js apps/api_gateway/app/static/index.html
git commit -m "feat(admin-ui): Pricing tab (per-model price entry) + embed kind in registry form"
```

---

## Phase 3 — Memory metering

### Task 10: Meter the per-turn query embedding

**Files:**
- Modify: `apps/api_gateway/app/services/memory/embedder.py`
- Modify: `apps/api_gateway/app/services/memory/retriever.py` (`get_context` lines 38-69, `_semantic_filter` lines 71-96)
- Test: `tests/unit/test_memory_usage_metering.py`

**Interfaces:**
- Produces:
  - `embed_texts_with_usage(texts, base_url, api_key, model) -> tuple[list[list[float]], int]` — vectors plus `usage.prompt_tokens` (`0` when the provider omits it). Raises on HTTP failure, exactly like `embed_texts`.
  - `embed_texts(...)` keeps its current signature and return type (a thin wrapper), so existing callers and tests stay valid.
  - `MemoryRetriever._semantic_filter(items, query, profile, user_id)` — gains a 4th parameter.

**Why this call site matters most:** it runs on *every* turn in `mode="semantic"`, unlike extraction/compaction which run once per session.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_memory_usage_metering.py`:

```python
import httpx
import pytest
from sqlalchemy import select

from app.services.db.engine import db_session, init_db
from app.services.db.models import UsageEvent
from app.services.memory.retriever import MemoryRetriever
from app.services.memory.store import memory_store
from app.services.profiles.models import Profile


async def _usage_rows(kind=None):
    async with db_session() as s:
        rows = (await s.execute(select(UsageEvent))).scalars().all()
    return [r for r in rows if kind is None or r.kind == kind]


@pytest.fixture
def _fake_embeddings(monkeypatch):
    async def fake_post(self, url, headers=None, json=None):
        class R:
            def raise_for_status(self):
                pass

            def json(self):
                n = len(json.get("input") or [])
                return {
                    "data": [{"embedding": [1.0, 0.0]} for _ in range(n)],
                    "usage": {"prompt_tokens": 7 * n},
                }

        return R()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


@pytest.mark.asyncio
async def test_embed_texts_with_usage_returns_prompt_tokens(_fake_embeddings):
    from app.services.memory.embedder import embed_texts, embed_texts_with_usage

    vecs, tokens = await embed_texts_with_usage(["a", "b"], "http://llm.local/v1", "k", "emb")
    assert len(vecs) == 2 and tokens == 14
    # The old signature still works for callers that don't meter.
    assert len(await embed_texts(["a"], "http://llm.local/v1", "k", "emb")) == 1


@pytest.mark.asyncio
async def test_missing_usage_block_counts_as_zero_tokens(monkeypatch):
    async def fake_post(self, url, headers=None, json=None):
        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"data": [{"embedding": [0.5]}]}  # no "usage" key at all

        return R()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    from app.services.memory.embedder import embed_texts_with_usage

    vecs, tokens = await embed_texts_with_usage(["a"], "http://llm.local/v1", "k", "emb")
    assert len(vecs) == 1 and tokens == 0


@pytest.mark.asyncio
async def test_semantic_retrieval_records_embed_usage(_fake_embeddings):
    await init_db()
    await memory_store.add("metering", "user likes tea", embedding=[1.0, 0.0], user_id="u9")
    profile = Profile(
        name="metering",
        llm={"base_url": "http://llm.local/v1", "model": "m", "engine": "openai"},
        memory={"mode": "semantic", "embed_model": "text-embedding-3-small"},
    )
    await MemoryRetriever().get_context(profile, query="trà", user_id="u9")

    rows = await _usage_rows("embed")
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id == "u9"
    assert row.profile_id == "metering"
    assert row.engine == "openai"
    assert row.model_id == "text-embedding-3-small"
    assert row.unit == "tokens"
    assert row.native_amount == 7 and row.prompt_tokens == 7


@pytest.mark.asyncio
async def test_retrieval_still_works_when_metering_blows_up(monkeypatch, _fake_embeddings):
    await init_db()
    await memory_store.add("metering2", "fact", embedding=[1.0, 0.0])

    async def boom(**kwargs):
        raise RuntimeError("recorder down")

    monkeypatch.setattr("app.services.memory.retriever.record_usage", boom)
    profile = Profile(
        name="metering2",
        llm={"base_url": "http://llm.local/v1", "model": "m", "engine": "openai"},
        memory={"mode": "semantic", "embed_model": "e"},
    )
    # Must NOT raise, and must still return the memory block.
    assert "fact" in await MemoryRetriever().get_context(profile, query="q")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_memory_usage_metering.py -q`
Expected: FAIL — `ImportError: cannot import name 'embed_texts_with_usage'`.

- [ ] **Step 3: Split the embedder into a usage-reporting core plus a wrapper**

Replace `embed_texts` in `apps/api_gateway/app/services/memory/embedder.py` with:

```python
async def embed_texts_with_usage(
    texts: list[str], base_url: str, api_key: str, model: str
) -> tuple[list[list[float]], int]:
    """Embed texts via an OpenAI-compatible /embeddings endpoint, also returning
    the provider's reported prompt_tokens (0 when it reports none) so the caller
    can meter the spend. Raises on failure."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    timeout = system_config_store.get().conversation.llm_timeout_seconds
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/embeddings",
            headers=headers,
            json={"model": model, "input": texts},
        )
        resp.raise_for_status()
        body = resp.json()
    vectors = [d["embedding"] for d in body["data"]]
    tokens = int((body.get("usage") or {}).get("prompt_tokens") or 0)
    return vectors, tokens


async def embed_texts(
    texts: list[str], base_url: str, api_key: str, model: str
) -> list[list[float]]:
    """Vectors only -- for callers with no identity to attribute usage to (e.g.
    the Model Registry's add-time test call)."""
    vectors, _tokens = await embed_texts_with_usage(texts, base_url, api_key, model)
    return vectors
```

- [ ] **Step 4: Meter the query embedding in the retriever**

In `apps/api_gateway/app/services/memory/retriever.py`, update the imports:

```python
from app.services.memory.embedder import cosine, embed_texts_with_usage
from app.services.memory.store import memory_store, profile_doc_store
from app.services.profiles.models import Profile
from app.services.usage.recorder import record_usage
```

In `get_context`, pass the caller's identity down (line 48):
```python
        if profile.memory.mode == "semantic" and query and items:
            items = await self._semantic_filter(items, query, profile, user_id)
```

Rewrite `_semantic_filter`:
```python
    async def _semantic_filter(
        self, items: list[dict], query: str, profile: Profile, user_id: str | None = None
    ) -> list[dict]:
        """Top-k by cosine similarity; falls back to the full list on any gap."""
        with_vec = [i for i in items if i.get("embedding")]
        if not with_vec or not profile.memory.embed_model or not profile.llm.base_url:
            logger.warning(
                "semantic memory mode for profile %s falling back to all: %s",
                profile.name,
                "no stored embeddings" if not with_vec else "embed_model/base_url not configured",
            )
            return items
        try:
            vectors, tokens = await embed_texts_with_usage(
                [query], profile.llm.base_url, profile.llm.api_key,
                profile.memory.embed_model,
            )
            qvec = vectors[0]
        except Exception as exc:  # noqa: BLE001 - fall back to all memories
            logger.warning("semantic memory embed failed, using all: %s", exc)
            return items
        # This runs on EVERY semantic-mode turn, so it's the embedding spend
        # that actually adds up. Metered after the call succeeds, never before.
        try:
            await record_usage(
                user_id=user_id or "", profile_id=profile.name,
                kind="embed", engine=profile.llm.engine or "",
                model_id=profile.memory.embed_model, unit="tokens",
                native_amount=tokens, prompt_tokens=tokens,
            )
        except Exception as exc:  # noqa: BLE001 - metering must never break retrieval
            logger.warning("memory query embed metering failed: %s", exc)
        scored = sorted(
            with_vec, key=lambda i: cosine(qvec, i["embedding"]), reverse=True
        )
        return scored[: profile.memory.top_k]
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/unit/test_memory_usage_metering.py tests/unit/test_memory_retriever.py tests/unit/test_memory_extractor.py -q`
Expected: PASS. The two existing files must stay green — they call `embed_texts`, which is why the wrapper was kept.

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/services/memory/embedder.py apps/api_gateway/app/services/memory/retriever.py tests/unit/test_memory_usage_metering.py
git commit -m "feat(usage): meter the per-turn memory query embedding"
```

---

### Task 11: Meter the extractor's LLM call and fact embedding

**Files:**
- Modify: `apps/api_gateway/app/services/memory/extractor.py` (`extract` lines 55-86, `_maybe_embed` lines 88-109, `extract_and_upsert` lines 111-157)
- Test: `tests/unit/test_memory_usage_metering.py` (append)

**Interfaces:**
- Consumes: `record_usage`, `embed_texts_with_usage` (Task 10).
- Produces: `MemoryExtractor.extract(messages, base_url, api_key, model, *, user_id="", profile_id="", engine="")` — the three new parameters are keyword-only with defaults, so existing calls in `tests/unit/test_memory_extractor.py` keep working. `_maybe_embed(profile, texts, user_id=None)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_memory_usage_metering.py`:

```python
@pytest.mark.asyncio
async def test_extractor_meters_its_llm_call_and_its_embedding(monkeypatch):
    await init_db()
    from app.services.history.store import session_store
    from app.services.memory.extractor import MemoryExtractor

    await session_store.create("s-meter", profile_id="ex")
    await session_store.append_message("s-meter", 1, "user", "tôi thích trà")
    await session_store.append_message("s-meter", 1, "assistant", "ok")

    async def fake_post(self, url, headers=None, json=None):
        class R:
            def raise_for_status(self):
                pass

            def json(self):
                if url.endswith("/embeddings"):
                    n = len(json.get("input") or [])
                    return {
                        "data": [{"embedding": [0.1]} for _ in range(n)],
                        "usage": {"prompt_tokens": 5 * n},
                    }
                return {
                    "choices": [{"message": {"content": '["User likes tea"]'}}],
                    "usage": {"prompt_tokens": 120, "completion_tokens": 8},
                }

        return R()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    profile = Profile(
        name="ex",
        llm={"base_url": "http://llm.local/v1", "model": "m", "engine": "openai"},
        memory={"embed_model": "text-embedding-3-small"},
    )
    added = await MemoryExtractor().extract_and_upsert("s-meter", profile, user_id="u7")
    assert added == 1

    llm_rows = [r for r in await _usage_rows("llm") if r.profile_id == "ex"]
    assert len(llm_rows) == 1
    assert llm_rows[0].prompt_tokens == 120 and llm_rows[0].completion_tokens == 8
    assert llm_rows[0].native_amount == 128
    assert llm_rows[0].user_id == "u7" and llm_rows[0].engine == "openai"
    assert llm_rows[0].model_id == "m"

    embed_rows = [r for r in await _usage_rows("embed") if r.profile_id == "ex"]
    assert len(embed_rows) == 1
    assert embed_rows[0].prompt_tokens == 5
    assert embed_rows[0].model_id == "text-embedding-3-small"


@pytest.mark.asyncio
async def test_extractor_uses_the_extractor_model_id_when_set(monkeypatch):
    await init_db()
    from app.services.history.store import session_store
    from app.services.memory.extractor import MemoryExtractor

    await session_store.create("s-model", profile_id="exm")
    await session_store.append_message("s-model", 1, "user", "hi there")
    await session_store.append_message("s-model", 1, "assistant", "hello")

    async def fake_post(self, url, headers=None, json=None):
        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "choices": [{"message": {"content": '["User says hi"]'}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                }

        return R()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    profile = Profile(
        name="exm",
        llm={"base_url": "http://llm.local/v1", "model": "chat-model", "engine": "openai"},
        memory={"extractor_model": "cheap-model"},
    )
    await MemoryExtractor().extract_and_upsert("s-model", profile, user_id="u8")
    rows = [r for r in await _usage_rows("llm") if r.profile_id == "exm"]
    # Attribution must name the model that was actually billed, not the chat one.
    assert [r.model_id for r in rows] == ["cheap-model"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_memory_usage_metering.py -q -k extractor`
Expected: FAIL — `assert len(llm_rows) == 1` with 0 rows.

- [ ] **Step 3: Meter the extractor's LLM call**

In `apps/api_gateway/app/services/memory/extractor.py`, replace the embedder import line and add the recorder:

```python
from app.services.memory.embedder import cosine, embed_texts_with_usage
from app.services.usage.recorder import record_usage
```

Rewrite `MemoryExtractor.extract`:

```python
    async def extract(
        self, messages: list[dict], base_url: str, api_key: str, model: str,
        *, user_id: str = "", profile_id: str = "", engine: str = "",
    ) -> list[str]:
        transcript = "\n".join(
            f"{m['role']}: {m['content']}"
            for m in messages
            if m.get("role") in ("user", "assistant") and m.get("content")
        )
        if not transcript:
            return []
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        try:
            async with httpx.AsyncClient(
                timeout=system_config_store.get().conversation.llm_timeout_seconds
            ) as client:
                resp = await client.post(
                    f"{base_url.rstrip('/')}/chat/completions",
                    headers=headers,
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": EXTRACTION_PROMPT},
                            {"role": "user", "content": transcript},
                        ],
                    },
                )
                resp.raise_for_status()
                body = resp.json()
                content = body["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001 - extraction is best-effort
            logger.warning("memory extraction LLM call failed: %s", exc)
            return []
        # This is a real billable LLM call: without this row, post-session
        # memory work is spend that never shows up in usage/cost at all.
        try:
            usage = body.get("usage") or {}
            prompt_tokens = usage.get("prompt_tokens")
            completion_tokens = usage.get("completion_tokens")
            await record_usage(
                user_id=user_id, profile_id=profile_id, kind="llm", engine=engine,
                model_id=model, unit="tokens",
                native_amount=(prompt_tokens or 0) + (completion_tokens or 0),
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            )
        except Exception as exc:  # noqa: BLE001 - metering must never break extraction
            logger.warning("memory extraction usage metering failed: %s", exc)
        return _parse_facts(str(content))
```

- [ ] **Step 4: Meter the fact embedding**

Rewrite `_maybe_embed`:

```python
    async def _maybe_embed(
        self, profile: Profile, texts: list[str], user_id: str | None = None
    ) -> list[list[float] | None]:
        """Embed texts when an embed_model is configured; else all None. Best-effort."""
        if not texts or not profile.memory.embed_model or not profile.llm.base_url:
            return [None] * len(texts)
        try:
            vecs, tokens = await embed_texts_with_usage(
                texts, profile.llm.base_url, profile.llm.api_key,
                profile.memory.embed_model,
            )
        except Exception as exc:  # noqa: BLE001 - embedding is best-effort
            logger.warning("memory embed failed: %s", exc)
            return [None] * len(texts)
        try:
            await record_usage(
                user_id=user_id or "", profile_id=profile.name, kind="embed",
                engine=profile.llm.engine or "", model_id=profile.memory.embed_model,
                unit="tokens", native_amount=tokens, prompt_tokens=tokens,
            )
        except Exception as exc:  # noqa: BLE001 - metering must never break extraction
            logger.warning("memory embed metering failed: %s", exc)
        if len(vecs) != len(texts):
            logger.warning(
                "memory embed length mismatch: got %d vectors for %d texts; "
                "storing facts without embeddings instead of dropping any",
                len(vecs), len(texts),
            )
            return [None] * len(texts)
        return vecs
```

- [ ] **Step 5: Pass identity from `extract_and_upsert`**

Change the `self.extract(...)` call (lines 122-124) and the `_maybe_embed` call (line 132):

```python
            facts = await self.extract(
                messages, profile.llm.base_url, profile.llm.api_key, model,
                user_id=user_id or "", profile_id=profile.name,
                engine=profile.llm.engine or "",
            )
```

```python
            new_vecs = await self._maybe_embed(profile, facts, user_id=user_id)
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest tests/unit/test_memory_usage_metering.py tests/unit/test_memory_extractor.py tests/unit/test_memory_compaction_e2e.py -q`
Expected: PASS. If a pre-existing test monkeypatches `MemoryExtractor.extract` with a 4-positional-arg stub it still matches (the new params are keyword-only); if one patches `_maybe_embed` with a 2-arg stub, add `user_id=None` to that stub.

- [ ] **Step 7: Commit**

```bash
git add apps/api_gateway/app/services/memory/extractor.py tests/unit/test_memory_usage_metering.py
git commit -m "feat(usage): meter memory extraction LLM + fact embedding"
```

---

### Task 12: Quota-gate post-session memory work (skip, never fail)

**Files:**
- Modify: `apps/api_gateway/app/services/memory/extractor.py` (`extract_and_upsert`, inside the existing `try` after `model` is computed; plus a new `_quota_blocked` method)
- Test: `tests/unit/test_memory_quota_gate.py`

**Interfaces:**
- Consumes: `quota_gate`, `QuotaExceededError` (`app.services.quota.gate`), `model_registry_store.find`.
- Produces: no new API. `extract_and_upsert` returns `0` without calling any provider when a quota applies.

**Contract:** the gate sits *before* the extraction LLM call, so it also suppresses the fact embedding and the compaction that follow in the same function. Over-quota memory work is skipped with a `logger.warning`; session teardown still succeeds. This is the one place a gate does *not* surface an error — nobody is waiting on this call.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_memory_quota_gate.py`:

```python
import httpx
import pytest

from app.services.db.engine import init_db
from app.services.history.store import session_store
from app.services.memory.extractor import MemoryExtractor
from app.services.memory.store import memory_store
from app.services.model_registry.store import model_registry_store
from app.services.profiles.models import Profile
from app.services.quota.store import quota_store
from app.services.usage.recorder import record_usage


@pytest.fixture
def _no_llm_calls(monkeypatch):
    """Any provider call at all is a test failure for the over-quota case."""
    async def boom(self, url, headers=None, json=None):
        raise AssertionError(f"provider was called while over quota: {url}")

    monkeypatch.setattr(httpx.AsyncClient, "post", boom)


async def _seed_session(session_id, profile_name):
    await session_store.create(session_id, profile_id=profile_name)
    await session_store.append_message(session_id, 1, "user", "tôi thích trà")
    await session_store.append_message(session_id, 1, "assistant", "ok")


@pytest.mark.asyncio
async def test_over_quota_skips_extraction_entirely(_no_llm_calls):
    await init_db()
    quota_store.invalidate()
    await _seed_session("s-quota", "qp")
    await model_registry_store.create(
        "llm", "openai", "m-quota", "priced",
        config={"provider_id": "prov-q", "price": {"unit": "1M_tokens", "in": 1000.0, "out": 0.0}},
    )
    # 1M tokens at $1000/1M = $1000 of spend against a $1 limit.
    await record_usage(user_id="u-quota", profile_id="qp", kind="llm", engine="openai",
                       model_id="m-quota", unit="tokens", native_amount=1_000_000,
                       prompt_tokens=1_000_000)
    await quota_store.create(scope="user", scope_id="u-quota", limit_usd=1.0, period="monthly")

    profile = Profile(
        name="qp",
        llm={"base_url": "http://llm.local/v1", "model": "m-quota", "engine": "openai"},
    )
    assert await MemoryExtractor().extract_and_upsert("s-quota", profile, user_id="u-quota") == 0
    assert await memory_store.list("qp", user_id="u-quota") == []


@pytest.mark.asyncio
async def test_under_quota_still_extracts(monkeypatch):
    await init_db()
    quota_store.invalidate()
    await _seed_session("s-under", "up")

    async def fake_post(self, url, headers=None, json=None):
        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "choices": [{"message": {"content": '["User likes tea"]'}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 3},
                }

        return R()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    await quota_store.create(scope="user", scope_id="u-under", limit_usd=100.0, period="monthly")
    profile = Profile(
        name="up",
        llm={"base_url": "http://llm.local/v1", "model": "m", "engine": "openai"},
    )
    assert await MemoryExtractor().extract_and_upsert("s-under", profile, user_id="u-under") == 1


@pytest.mark.asyncio
async def test_gate_failure_fails_open(monkeypatch):
    await init_db()
    quota_store.invalidate()
    await _seed_session("s-open", "op")

    async def gate_boom(**kwargs):
        raise RuntimeError("quota subsystem down")

    monkeypatch.setattr("app.services.quota.gate.quota_gate", gate_boom)

    async def fake_post(self, url, headers=None, json=None):
        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": '["User likes tea"]'}}]}

        return R()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    profile = Profile(
        name="op",
        llm={"base_url": "http://llm.local/v1", "model": "m", "engine": "openai"},
    )
    # A broken gate must not stop memory work.
    assert await MemoryExtractor().extract_and_upsert("s-open", profile, user_id="u-open") == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_memory_quota_gate.py -q`
Expected: FAIL on the first test — `AssertionError: provider was called while over quota: http://llm.local/v1/chat/completions`.

- [ ] **Step 3: Add the gate**

In `extract_and_upsert`, insert between `model = profile.memory.extractor_model or profile.llm.model` (line 121) and the `facts = await self.extract(...)` call:

```python
            # Post-session memory work is real provider spend, so it goes
            # through the same gate as a turn -- but nobody is waiting on it, so
            # over-quota means "skip and log", never an error to a caller.
            if await self._quota_blocked(profile, model, user_id):
                return 0
```

Add this method to `MemoryExtractor`, above `extract_and_upsert`:

```python
    async def _quota_blocked(
        self, profile: Profile, model: str, user_id: str | None
    ) -> bool:
        """True when an applicable quota is already over its limit. Resolving
        provider_id is wrapped separately so a registry hiccup degrades to
        user/global-scope enforcement rather than blocking or crashing."""
        from app.services.model_registry.store import model_registry_store
        from app.services.quota.gate import QuotaExceededError, quota_gate

        provider_id = ""
        try:
            engine = profile.llm.engine or ""
            if engine:
                entry = await model_registry_store.find("llm", engine, model)
                provider_id = (entry or {}).get("config", {}).get("provider_id", "") if entry else ""
        except Exception:  # noqa: BLE001 - never block memory on a lookup
            provider_id = ""
        try:
            await quota_gate(user_id=user_id or "", provider_id=provider_id)
        except QuotaExceededError as exc:
            logger.warning("memory extraction skipped for %s: %s", profile.name, exc)
            return True
        except Exception as exc:  # noqa: BLE001 - fail-open, same as quota_gate itself
            logger.warning("memory quota check failed open for %s: %s", profile.name, exc)
        return False
```

The local import of `quota_gate` is deliberate — it mirrors `session.py:433` and the REST routes, and keeps `extractor.py` importable without the quota subsystem.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/unit/test_memory_quota_gate.py tests/unit/test_memory_extractor.py tests/unit/test_memory_usage_metering.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/memory/extractor.py tests/unit/test_memory_quota_gate.py
git commit -m "feat(quota): skip post-session memory work when over quota"
```

---

### Task 13: Meter the compactor's LLM call

**Files:**
- Modify: `apps/api_gateway/app/services/memory/compactor.py` (`_call_llm` lines 40-70, `compact` line 99)
- Test: `tests/unit/test_memory_usage_metering.py` (append)

**Interfaces:**
- Consumes: `record_usage`.
- Produces: `MemoryCompactor._call_llm(profile, current_doc, facts, user_id=None)`; `compact` / `maybe_compact` signatures unchanged.

**Note:** `maybe_compact` is only ever called from `extract_and_upsert` (`extractor.py:153`), which Task 12 already gated — so compaction needs metering but no gate of its own.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_memory_usage_metering.py`:

```python
@pytest.mark.asyncio
async def test_compactor_meters_its_llm_call(monkeypatch):
    await init_db()
    from app.services.memory.compactor import MemoryCompactor

    for i in range(3):
        await memory_store.add("cmp", f"fact {i}", user_id="u5")

    async def fake_post(self, url, headers=None, json=None):
        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "choices": [{"message": {"content": "## User Profile\n### Danh tính\n- x"}}],
                    "usage": {"prompt_tokens": 300, "completion_tokens": 40},
                }

        return R()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    profile = Profile(
        name="cmp",
        llm={"base_url": "http://llm.local/v1", "model": "chat", "engine": "openai"},
        memory={"extractor_model": "cheap", "compaction_threshold": 2},
    )
    assert await MemoryCompactor().maybe_compact(profile, user_id="u5") is True

    rows = [r for r in await _usage_rows("llm") if r.profile_id == "cmp"]
    assert len(rows) == 1
    assert rows[0].model_id == "cheap"          # the model actually billed
    assert rows[0].engine == "openai"
    assert rows[0].user_id == "u5"
    assert rows[0].prompt_tokens == 300 and rows[0].completion_tokens == 40
    assert rows[0].native_amount == 340
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_memory_usage_metering.py -q -k compactor`
Expected: FAIL — 0 rows.

- [ ] **Step 3: Meter the compaction call**

In `apps/api_gateway/app/services/memory/compactor.py`, add the import:

```python
from app.services.usage.recorder import record_usage
```

Change `_call_llm` to take `user_id` and record after success:

```python
    async def _call_llm(
        self, profile: Profile, current_doc: str, facts: list[str],
        user_id: str | None = None,
    ) -> str:
        prompt = (
            "CURRENT PROFILE:\n"
            + (current_doc or "(empty)")
            + "\n\nNEW FACTS (oldest first):\n"
            + "\n".join(f"- {f}" for f in facts)
        )
        headers = (
            {"Authorization": f"Bearer {profile.llm.api_key}"}
            if profile.llm.api_key
            else {}
        )
        async with httpx.AsyncClient(
            timeout=system_config_store.get().conversation.llm_timeout_seconds
        ) as client:
            resp = await client.post(
                f"{profile.llm.base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json={
                    "model": self._model(profile),
                    "messages": [
                        {"role": "system", "content": COMPACTION_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
            resp.raise_for_status()
            body = resp.json()
            content = body["choices"][0]["message"]["content"]
        # Compaction sends the whole fact buffer -- the most expensive single
        # memory call there is. Meter it.
        try:
            usage = body.get("usage") or {}
            prompt_tokens = usage.get("prompt_tokens")
            completion_tokens = usage.get("completion_tokens")
            await record_usage(
                user_id=user_id or "", profile_id=profile.name, kind="llm",
                engine=profile.llm.engine or "", model_id=self._model(profile),
                unit="tokens",
                native_amount=(prompt_tokens or 0) + (completion_tokens or 0),
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            )
        except Exception as exc:  # noqa: BLE001 - metering must never break compaction
            logger.warning("memory compaction usage metering failed: %s", exc)
        return str(content).strip()
```

In `compact`, pass the identity through (line 99):

```python
        new_doc = (await self._call_llm(profile, current_doc, facts, user_id=user_id) or "").strip()
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/unit/test_memory_usage_metering.py tests/unit/test_memory_compactor.py tests/unit/test_memory_compaction_e2e.py -q`
Expected: PASS. If an existing test stubs `_call_llm` with a 3-positional-arg replacement, add `user_id=None` to that stub.

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/memory/compactor.py tests/unit/test_memory_usage_metering.py
git commit -m "feat(usage): meter memory compaction LLM call"
```

---

### Task 14: Full-suite gate + docs

**Files:**
- Modify: `docs/superpowers/specs/2026-07-23-provider-management-usage-quota-design.md`

- [ ] **Step 1: Run the whole backend suite**

Run: `.venv/bin/python -m pytest tests/unit -q`
Expected: all pass. Baseline before this plan was 1254 passing; the new files add roughly 45 tests. If anything unrelated fails, clear stale bytecode (a known gotcha here) and re-run: `find apps tests -name __pycache__ -prune -exec rm -rf {} +`.

- [ ] **Step 2: Verify the app imports and the JS parses**

```bash
.venv/bin/python -c "import app.main"
node --check apps/api_gateway/app/static/js/pricing.js
node --check apps/api_gateway/app/static/js/sidebar-nav.js
node --check apps/api_gateway/app/static/js/model-registry.js
node --check apps/api_gateway/app/static/js/usage.js
node --check apps/api_gateway/app/static/js/usage-me.js
```
Expected: no output from any of them.

- [ ] **Step 3: Record what shipped in the design doc**

Append to `docs/superpowers/specs/2026-07-23-provider-management-usage-quota-design.md`:

```markdown
## 13. Trạng thái triển khai (cập nhật 2026-07-26)

Đã bổ sung theo plan `plans/2026-07-26-usage-cost-p0.md`:
- `usage/attribution.py`: resolve `(engine, model_id)` khi ghi usage — hết
  `(none)`; quan trọng hơn: row có model rỗng KHÔNG khớp được registry row giữ
  giá nên trước đây luôn $0. Chỉ suy ra khi chắc chắn (engine có đúng 1 model
  non-sentinel), không đoán.
- `usage/backfill.py` (chạy lúc boot): backfill model_id cho row cũ khi suy được
  (270/307 row trên prod); không bao giờ tính lại `cost_usd` lịch sử.
- LLM usage lấy model từ `responder.model` (model thật đã gọi), không phải pin
  của profile.
- `/v1/usage/me` group thêm theo `engine`; UI hiện cột Engine và nhãn
  `(not recorded)` cho row không suy được.
- `usage/price_schema.py`: validate/normalize `config.price` khi ghi (unit suy
  từ kind, chặn field lạ / số âm / bool), áp cho cả POST/PATCH model_registry.
- `GET/PATCH /v1/model_registry/prices` + tab admin "Pricing".
- `kind="embed"` thành kind chính thức của Model Registry.
- Đo usage 4 call site của memory: extractor LLM, compactor LLM, embed facts,
  embed query mỗi lượt (`kind="embed"`).
- `quota_gate` cho memory hậu-session: vượt hạn mức thì bỏ qua + log.

Còn thiếu (xem audit 2026-07-26, nhóm P1/P2): audit row `status="blocked"`;
metering/gate cho `POST /v1/tts/stream` và `WS /v1/stt/stream`; `profile_id=""`
ở REST; hiển thị spend/limit trên tab Quotas và My Usage; rollup
`usage_counters`; validate quota (scope_id, trùng, limit<=0).
```

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-07-23-provider-management-usage-quota-design.md
git commit -m "docs: record usage/cost P0 implementation status"
```

- [ ] **Step 5: Report, do not push**

Report the final test count and the commit list. `main` auto-deploys and this branch is not merged — merging and pushing is the user's call.

---

## Self-Review

**1. Coverage of what was asked:**
- *`(none)` rows in My Usage and Usage; "no stt/tts/llm request exists without a model"* → Task 1 (resolver), Task 2 (applied to every row at the recorder + the STT route's ignored `model` field), Task 3 (LLM's real model from the responder), Task 4 (backfill of 270 legacy rows), Task 5 (both views: engine column + honest `(not recorded)` for the 37 unprovable ones).
- *No UI to enter `config.price`; raw-JSON only; no validation* → Tasks 6, 7, 9.
- *Embeddings have no priceable registry kind* → Task 8.
- *`memory/extractor.py:71`, `memory/compactor.py:58`, `memory/embedder.py:18` unmetered* → Tasks 11, 13, 10 (10 also covers the per-turn query embed found while planning, which was not in the original audit list).
- *Memory calls ungated* → Task 12.
- Pre-plan decisions honored: separate Pricing tab, `embed` as a first-class kind, over-quota memory work skips silently.

**2. Placeholder scan:** every step carries literal code or commands. Remaining judgment calls are named and bounded: which pre-existing assertions may need comment updates (Task 2 Step 5, with the expected outcome and the fallback stated), the `DATABASE_URL` env name for the copy-DB check (Task 4 Step 6, with a skip path), and stub arity fixes in older memory tests (Tasks 11/13).

**3. Type/name consistency:** `resolve_usage_model(kind, engine, model_id) -> (engine, model_id)` is defined in Task 1 and consumed in Task 2 with that exact shape. `validate_price` / `apply_price_to_config` / `PRICE_UNIT_BY_KIND` are defined in Task 6 and used identically in Tasks 7-8. `embed_texts_with_usage(...) -> (vectors, tokens)` is defined in Task 10 and consumed with that shape in Task 11. Every `record_usage(...)` call matches the keyword-only signature at `usage/recorder.py:14`. `kind="embed"` is used identically by the registry (Task 8) and the metering call sites (Tasks 10-11), which is what makes those rows priceable. `migrate_backfill_usage_model_ids()` is defined in Task 4 and registered in `main.py` in the same task.

**4. Ordering:** Phase 1 before Phase 2 is load-bearing (a blank `model_id` can't match a priced registry row). Within Phase 1: 1 → 2 → 3, then 4 (needs the registry as source of truth), then 5. Phase 2: 6 → 7 → 8 → 9. Phase 3: 10 → 11 → 12 (12 edits the function 11 touches); 13 is independent apart from the shared test file. Tasks 5 and 9 are the only UI tasks; per this repo's convention their verification is `node --check` plus a Read-back rather than automated tests.
