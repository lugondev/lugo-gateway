# Provider Quota Enforcement Implementation Plan (Plan 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Let an admin set USD spend limits per user / per provider / globally (monthly or lifetime) and **block** further LLM/STT/TTS requests once a limit is reached, using the usage already recorded by Plan 2.

**Architecture:** A `quotas` table + a cached `QuotaStore`. A `quota_gate(user_id, provider_id)` pre-flight check sums `usage_events.cost_usd` for each applicable enabled quota's current period and raises `QuotaExceededError` if any is at/over its limit. Gate is **fail-OPEN**: any internal error (bad query, etc.) logs and ALLOWS the request — a quota bug must never deny service; only a genuine confirmed over-limit blocks. Enforcement is pre-flight (before the provider call) at the REST routes (→ HTTP 429) and once per turn in the conversation core (→ abort the turn with a clear notice). Because usage is recorded *after* each call, the model is "block once already over" (the request that crosses the line succeeds; the next is blocked) — the accepted best-effort model from the spec. Admin CRUD API + a Quota admin UI tab.

**Tech Stack:** FastAPI, SQLAlchemy async (`db_session`, `func.sum`), reuse `usage/query.py::_period_range`, pytest, static ES-module admin UI.

**Spec:** `docs/superpowers/specs/2026-07-management-usage-quota-design.md` §7 (quota). Builds on Plan 2 (`usage_events`, `record_usage`) + Provider feature — all merged to local main.

## Global Constraints
- Python 3.12 venv; tests from repo ROOT `.venv/bin/python -m pytest tests/unit/<f> -v`; `asyncio_mode="auto"`; `_tmp_db` autouse (NEVER a param); sync `TestClient`.
- New table only (`create_all`). No ALTER.
- **Fail-open:** `quota_gate` catches every non-`QuotaExceededError` exception, logs, and returns (allows). Never deny service on a gate bug. `QuotaExceededError` is the ONLY thing it raises.
- **Spend source = SUM over `usage_events`** for the period (not a separate counters table — justified: accurate, simple, fine at this deployment scale; a rollup is a later optimization if volume demands).
- Scopes: `user` (scope_id=user_id), `provider` (scope_id=provider_id), `global` (scope_id=""). Period: `monthly` (current "YYYY-MM") or `total` (all-time).
- Enforcement must not itself break a turn beyond the intended block: gate is fail-open; the core translates a real block into a user-facing notice + skipped turn (not a crash), REST into 429.
- `/v1/quotas` admin-gated (auth_guard `_ADMIN_PREFIXES`). Quota UI nav-item `admin-only`.
- Git `lugondev <lugondev@gmail.com>`. No submodules/.dockerignore. No push (main auto-deploys prod).

---

### Task 1: `Quota` model + `QuotaStore`

**Files:**
- Modify: `apps/api_gateway/app/services/db/models.py` (append after `UsageEvent`)
- Create: `apps/api_gateway/app/services/quota/__init__.py` (empty), `apps/api_gateway/app/services/quota/store.py`
- Modify: `tests/conftest.py` (invalidate quota_store alongside provider_store/model_registry_store)
- Test: `tests/unit/test_quota_store.py`

**Interfaces:**
- `Quota` ORM (table `quotas`): `id, scope, scope_id, limit_usd, period, enabled`.
- singleton `quota_store` (mirror `providers/store.py`): `list_all()`, `list_enabled()`, `get(id)`, `create(scope, scope_id, limit_usd, period="monthly", enabled=True)`, `set_fields(id, **f)`, `delete(id)`, `invalidate()`. Dicts: `{id, scope, scope_id, limit_usd, period, enabled}`.

- [ ] **Step 1: Write failing test**

```python
# tests/unit/test_quota_store.py
from app.services.db.engine import init_db
from app.services.quota.store import quota_store


async def test_create_list_enabled_and_delete():
    await init_db()
    q = await quota_store.create(scope="user", scope_id="u1", limit_usd=10.0, period="monthly")
    assert q["scope"] == "user" and q["limit_usd"] == 10.0 and q["enabled"] is True
    enabled = await quota_store.list_enabled()
    assert any(e["id"] == q["id"] for e in enabled)
    await quota_store.set_fields(q["id"], enabled=False)
    assert all(e["id"] != q["id"] for e in await quota_store.list_enabled())
    assert await quota_store.delete(q["id"]) is True
    assert await quota_store.get(q["id"]) is None
```

- [ ] **Step 2: Run — FAIL** (`.venv/bin/python -m pytest tests/unit/test_quota_store.py -v`).

- [ ] **Step 3: Add the model** (after `UsageEvent` in models.py; `Float`/`Integer`/`String`/`Boolean` already imported):

```python
class Quota(Base):
    __tablename__ = "quotas"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scope: Mapped[str] = mapped_column(String(16), index=True)   # user|provider|global
    # user_id | provider_id | "" (global)
    scope_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    limit_usd: Mapped[float] = mapped_column(Float, default=0.0)
    period: Mapped[str] = mapped_column(String(16), default="monthly")  # monthly|total
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
```

- [ ] **Step 4: Implement `store.py`** — copy `apps/api_gateway/app/services/providers/store.py` structure (cache dict keyed by id, `_ensure_loaded`, `invalidate` replacing the Lock, `_copy`, async CRUD). Add `list_enabled()` returning only `enabled` entries. Fields per `_entry_dict`: id/scope/scope_id/limit_usd/period/enabled. `create(scope, scope_id="", limit_usd=0.0, period="monthly", enabled=True)`. Singleton `quota_store = QuotaStore()`.

- [ ] **Step 5: conftest** — mirror the provider_store lines: `from app.services.quota.store import quota_store` and `quota_store.invalidate()` after BOTH `provider_store.invalidate()` calls (pre- and post-yield).

- [ ] **Step 6: Run — PASS.** Full suite once: `.venv/bin/python -m pytest tests/unit -q` (no regressions).

- [ ] **Step 7: Commit** — `git add` models.py, quota/, conftest.py, test → `feat(quota): Quota model + QuotaStore`.

---

### Task 2: `quota_gate` + `QuotaExceededError` (fail-open spend check)

**Files:**
- Create: `apps/api_gateway/app/services/quota/gate.py`
- Test: `tests/unit/test_quota_gate.py`

**Interfaces:**
- `class QuotaExceededError(Exception)`: attrs `scope, scope_id, limit_usd, spend_usd, period`; `__str__` → human message.
- `async def current_spend(*, scope: str, scope_id: str, period: str) -> float` — SUM `usage_events.cost_usd` filtered by scope (user→`user_id==scope_id`, provider→`provider_id==scope_id`, global→no filter) and, when `period=="monthly"`, `ts` within the current month (reuse `usage/query.py::_period_range` with the current "YYYY-MM"); `total` → all time. Returns 0.0 if none.
- `async def quota_gate(*, user_id: str, provider_id: str) -> None` — for each enabled quota that APPLIES (user & scope_id==user_id; provider & scope_id==provider_id and provider_id truthy; global), compute `current_spend` and raise `QuotaExceededError` if `spend >= limit_usd`. **Wrap everything except the raise in try/except that logs and returns (fail-open).**

- [ ] **Step 1: Write failing test**

```python
# tests/unit/test_quota_gate.py
import pytest
from app.services.db.engine import init_db, db_session
from app.services.db.models import UsageEvent
from app.services.quota.store import quota_store
from app.services.quota.gate import quota_gate, QuotaExceededError, current_spend


async def _add_cost(user_id, provider_id, cost):
    import uuid
    async with db_session() as s:
        s.add(UsageEvent(id=str(uuid.uuid4()), user_id=user_id, profile_id="", provider_id=provider_id,
                         kind="llm", engine="e", model_id="m", unit="tokens", native_amount=1, cost_usd=cost))
        await s.commit()


async def test_blocks_when_user_over_limit():
    await init_db()
    await quota_store.create(scope="user", scope_id="u1", limit_usd=1.0, period="total")
    await _add_cost("u1", "prov", 1.5)  # over
    with pytest.raises(QuotaExceededError):
        await quota_gate(user_id="u1", provider_id="prov")


async def test_allows_under_limit_and_other_user():
    await init_db()
    await quota_store.create(scope="user", scope_id="u1", limit_usd=10.0, period="total")
    await _add_cost("u1", "prov", 2.0)
    await quota_gate(user_id="u1", provider_id="prov")     # under → no raise
    await quota_gate(user_id="u2", provider_id="prov")     # different user, no quota → no raise


async def test_global_and_provider_scopes():
    await init_db()
    await quota_store.create(scope="global", scope_id="", limit_usd=1.0, period="total")
    await _add_cost("uX", "provA", 2.0)
    with pytest.raises(QuotaExceededError):
        await quota_gate(user_id="anyone", provider_id="provA")


async def test_fail_open_on_internal_error(monkeypatch):
    await init_db()
    async def boom(): raise RuntimeError("down")
    monkeypatch.setattr(quota_store, "list_enabled", boom)
    await quota_gate(user_id="u", provider_id="p")   # must NOT raise
```

- [ ] **Step 2: Run — FAIL.**

- [ ] **Step 3: Implement `gate.py`**

```python
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.services.db.engine import db_session
from app.services.db.models import UsageEvent
from app.services.quota.store import quota_store
from app.services.usage.query import _period_range

logger = logging.getLogger(__name__)


class QuotaExceededError(Exception):
    def __init__(self, scope, scope_id, limit_usd, spend_usd, period):
        self.scope, self.scope_id = scope, scope_id
        self.limit_usd, self.spend_usd, self.period = limit_usd, spend_usd, period
        super().__init__(
            f"{scope} quota exceeded"
            + (f" for {scope_id}" if scope_id else "")
            + f": ${spend_usd:.4f} / ${limit_usd:.4f} ({period})"
        )


def _current_month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


async def current_spend(*, scope: str, scope_id: str, period: str) -> float:
    stmt = select(func.coalesce(func.sum(UsageEvent.cost_usd), 0.0))
    if scope == "user":
        stmt = stmt.where(UsageEvent.user_id == scope_id)
    elif scope == "provider":
        stmt = stmt.where(UsageEvent.provider_id == scope_id)
    # global: no scope filter
    if period == "monthly":
        start, end = _period_range(_current_month_key())
        stmt = stmt.where(UsageEvent.ts >= start, UsageEvent.ts < end)
    async with db_session() as s:
        return float((await s.execute(stmt)).scalar_one() or 0.0)


def _applies(q: dict, user_id: str, provider_id: str) -> bool:
    if q["scope"] == "global":
        return True
    if q["scope"] == "user":
        return q["scope_id"] == (user_id or "")
    if q["scope"] == "provider":
        return bool(provider_id) and q["scope_id"] == provider_id
    return False


async def quota_gate(*, user_id: str, provider_id: str) -> None:
    """Pre-flight: raise QuotaExceededError if any applicable enabled quota is at/over
    its limit for the current period. FAIL-OPEN: any other error logs and allows."""
    try:
        quotas = await quota_store.list_enabled()
        for q in quotas:
            if not _applies(q, user_id, provider_id):
                continue
            spend = await current_spend(scope=q["scope"], scope_id=q["scope_id"], period=q["period"])
            if spend >= q["limit_usd"] > 0:
                raise QuotaExceededError(q["scope"], q["scope_id"], q["limit_usd"], spend, q["period"])
    except QuotaExceededError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail-open, never deny service on a gate bug
        logger.warning("quota_gate failed open: %s", exc)
```

- [ ] **Step 4: Run — PASS** (4 tests).
- [ ] **Step 5: Commit** — gate.py + test → `feat(quota): quota_gate spend check (fail-open) + QuotaExceededError`.

---

### Task 3: `/v1/quotas` admin CRUD API

**Files:** Create `apps/api_gateway/app/api/routes/quotas.py`; Modify `main.py` (register) + `core/auth_guard.py` (`/v1/quotas` → `_ADMIN_PREFIXES`). Test `tests/unit/test_quotas_routes.py`.

**Interfaces:** `GET /v1/quotas` (list), `POST /v1/quotas` (create: scope, scope_id, limit_usd, period, enabled), `PATCH /v1/quotas/{id}`, `DELETE /v1/quotas/{id}`. All `{success, data}`. Validate `scope in {user,provider,global}` and `period in {monthly,total}` (400 otherwise). Mirror `routes/providers.py` structure (no api_key/masking needed here).

- [ ] **Step 1: Failing test** (`test_quotas_routes.py`): admin can POST a quota + GET lists it + PATCH disable + DELETE; a non-admin gets 403 on GET; bad scope/period → 400. Use the admin-login helper pattern from `tests/unit/test_model_registry_routes.py`.
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** `quotas.py` (Pydantic `CreateQuotaRequest{scope, scope_id="", limit_usd, period="monthly", enabled=True}`, `UpdateQuotaRequest` all-optional; validate scope/period; call `quota_store`). Register router in `main.py` (import + include). Add `/v1/quotas` to `_ADMIN_PREFIXES` in `auth_guard.py`.
- [ ] **Step 4: Run — PASS**; full suite `.venv/bin/python -m pytest tests/unit -q`.
- [ ] **Step 5: Commit** — quotas.py + main.py + auth_guard.py + test → `feat(quota): /v1/quotas admin CRUD API`.

---

### Task 4: Enforce quota at REST routes (→ 429)

**Files:** Modify `apps/api_gateway/app/api/routes/conversation.py` (`/chat`), `stt.py` (`/transcribe`), `tts.py` (`/synthesize`), `livehost.py`. Test `tests/unit/test_quota_enforcement_routes.py`.

**Interfaces:** Consumes `quota_gate`, `QuotaExceededError`, `current_user_id`. At each route, BEFORE the provider work: resolve the model's `provider_id` (via `model_registry_store.find(kind, engine, model_id)` → `config.provider_id`, same lookup used elsewhere; `""` if none), then `await quota_gate(user_id=current_user_id(request) or "", provider_id=provider_id)`. Catch `QuotaExceededError` → `raise HTTPException(status_code=429, detail=str(exc))`. Do NOT catch other exceptions (gate is already fail-open internally).

- [ ] **Step 1: Read the routes** (`sed` the /chat, /transcribe, /synthesize handlers) to find where the resolved engine/model is in scope for the provider_id lookup, and the earliest safe point to gate (after auth/validation, before the provider call).
- [ ] **Step 2: Failing test**: create a `user`-scope quota with limit_usd tiny + seed a `usage_events` row over it for the test user, then call `/transcribe` (or `/synthesize`) as that user via TestClient (stub provider like existing route tests) → assert HTTP 429. And a control: under-limit → normal 200. (If wiring the exact provider_id is heavy, seed a `global`-scope quota so provider_id doesn't matter, and assert 429.)
- [ ] **Step 3: Run — FAIL.**
- [ ] **Step 4: Implement** the pre-flight gate + 429 at each route (conversation /chat, stt /transcribe, tts /synthesize; livehost if straightforward — else skip + note).
- [ ] **Step 5: Run — PASS**; regression `.venv/bin/python -m pytest tests/unit -q -k "stt or tts or conversation or livehost or quota"`.
- [ ] **Step 6: Commit** → `feat(quota): enforce quota at REST routes (429 on exceed)`.

---

### Task 5: Enforce quota in the conversation core (abort turn with notice)

**Files:** Modify `apps/api_gateway/app/services/conversation/session.py`. Test `tests/unit/test_quota_enforcement_core.py`.

**Interfaces:** Consumes `quota_gate`, `QuotaExceededError`. ONCE per turn, at the START of `_handle_turn`/`_run_turn` (before STT), call `await quota_gate(user_id=self.cfg.identity_user_id or "", provider_id=<the LLM provider_id for this turn>)` (resolve LLM provider_id via `model_registry_store.find("llm", profile.llm.engine, profile.llm.model).config.provider_id`; `""` if none — user & global scopes still apply). On `QuotaExceededError`: emit a user-facing notice (reuse the session's existing error/notice emit mechanism — find how other turn failures surface, e.g. an `emit("error"/"notice", ...)`), then RETURN early (skip the turn — no STT/LLM/TTS). The gate is fail-open, so only a real over-limit aborts; wrap the resolve-provider-id step so it can't raise (fall back to provider_id="").

- [ ] **Step 1: Read** `_handle_turn`/`_run_turn` start + how the session emits errors/notices to the client (grep `self.emit(` for an "error"/"notice"/"status" event) + where `profile.llm.engine/model` are accessible.
- [ ] **Step 2: Failing test**: build a `ConversationSession` (reuse the harness from `tests/unit/test_session_usage_metering.py`), seed a `global` (or user) quota over-limit for the session's user, drive one turn, assert: (a) the turn is aborted — no assistant reply / STT not run — and (b) a notice/error event was emitted. State the harness approach in the report.
- [ ] **Step 3: Run — FAIL.**
- [ ] **Step 4: Implement** the turn-start gate + notice + early return; fail-open on provider-id resolution.
- [ ] **Step 5: Run — PASS**; regression `.venv/bin/python -m pytest tests/unit -q -k "session or conversation or lugo or quota"`.
- [ ] **Step 6: Commit** → `feat(quota): abort conversation turn with a notice when over quota`.

---

### Task 6: Quota admin UI tab

**Files:** Create `apps/api_gateway/app/static/js/quotas.js`; Modify `index.html` (nav-item after Usage item + `#section-quotas`), `sidebar-nav.js`, `main.js`. (Static-UI only — node --check + grep, no pytest.)

**Interfaces:** `export async function loadQuotas()` — fetch `/v1/quotas`, render a table (scope, scope_id, limit_usd, period, enabled) with Edit/Enable-Disable/Delete; add-form (scope select user|provider|global, scope_id text, limit_usd number, period select monthly|total). Mirror `providers.js` (plain patterns; you MAY use `renderDataTable` with bulk enable/disable like providers, OR a plain table — match providers.js). nav-item `<li class="admin-only"> data-section="quotas"`; wire `loadQuotas()` in sidebar-nav + side-effect import in main.js.

- [ ] **Step 1: Read `providers.js` + the Providers nav-item/section in index.html** to mirror structure. Write `quotas.js` (CRUD against `/v1/quotas`; scope_id input hint: "user_id / provider_id / blank for global"). 
- [ ] **Step 2:** Add nav-item + section (after Usage), wire sidebar-nav + main.js.
- [ ] **Step 3: Verify** `node --check` on quotas.js/sidebar-nav.js/main.js + grep nav-item/section.
- [ ] **Step 4: Commit** → `feat(admin-ui): Quotas management tab`.

---

### Task 7: Full-suite gate (controller)
- [ ] `.venv/bin/python -m pytest tests/unit -q` (all pass) + `.venv/bin/python -c "import app.main"` + `node --check` on all touched JS.

---

## Deferred
- `usage_counters` rollup (only if SUM-over-events becomes slow at scale).
- Per-kind provider-scope gating in the core (currently core gates once with the LLM provider + user/global; STT/TTS provider-scope enforced at REST). Add per-kind core gating if needed.
- Showing remaining/used quota in the Usage dashboard.

## Self-Review
- **Spec §7 coverage:** quotas table + 3 scopes + monthly/total (T1); pre-flight SUM check + QuotaExceededError + fail-open (T2); admin CRUD (T3); hard-block REST→429 (T4) + core→abort-with-notice (T5); UI (T6). "Block once already over" model preserved (usage recorded post-call). Deviations noted: SUM-over-events instead of usage_counters; core gates once-per-turn with LLM provider (not per-kind).
- **Placeholder scan:** T1-T3 concrete; T4/T5 are enforcement in heavy existing files — explicit "read first", the exact gate contract, fail-open + notice invariants, and test-harness reuse pointers (not blanks).
- **Consistency:** `quota_gate(*, user_id, provider_id)` + `QuotaExceededError` signatures identical across T2 def and T4/T5 call sites; `quota_store` dict keys stable T1↔T2↔T3↔T6; scope/period enums identical everywhere.
