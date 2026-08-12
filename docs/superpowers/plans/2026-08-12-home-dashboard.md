# Home Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Home" tab to the admin console (`apps/api_gateway`) showing role-scoped stats: profile/device/session counts, usage & quota, and (admin-only) system health, active models, and a model registry summary.

**Architecture:** One new backend endpoint (`GET /v1/stats/home`) supplies the counts that have no existing total (profiles/devices/sessions), scoped by caller role using the same predicates the rest of the app already uses. Everything else is rendered from existing admin endpoints, called directly from a new `home.js`, exactly like `model-manager.js`/`system-status.js` already do elsewhere. Home becomes the default landing tab, replacing Conversation.

**Tech Stack:** FastAPI + SQLAlchemy (async) backend, vanilla JS ES modules frontend, pytest + `TestClient` for tests.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-12-home-dashboard-design.md` — read it before starting; this plan implements it exactly.
- Every new HTTP path must be classified in `app/core/auth_guard.py` (`_USER_PREFIXES` or `_ADMIN_PREFIXES`) or `tests/unit/http/test_auth_guard_route_coverage.py` fails — this is intentional, not a bug to work around.
- Commit as `lugondev <lugondev@gmail.com>` (project convention; never the Claude-account email).
- Run only the tests for the file(s) you touched while iterating; do not run the full suite until the final manual-verification task.
- "Active recently" (devices), never "Online" — `last_seen_at` is not a live heartbeat, see spec's caveat.

---

### Task 1: `SessionStore.count()`

**Files:**
- Modify: `apps/api_gateway/app/services/history/store.py:142` (insert a new method right after `list()` ends, before `append_message`)
- Test: `tests/unit/conversation/test_session_store.py`

**Interfaces:**
- Produces: `SessionStore.count(self, profile_id: str | None = None, user_id: str | None = None, source: str | None = None, client_id: str | None = None) -> int` — same filter semantics as `list()`, but returns a total instead of rows. Task 2 calls this as `session_store.count(user_id=...)`.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/conversation/test_session_store.py` (append at the end of the file):

```python
@pytest.mark.asyncio
async def test_count_matches_list_and_filters_by_user(store):
    await store.create("s1", user_id="u1")
    await store.create("s2", user_id="u1")
    await store.create("s3", user_id="u2")

    assert await store.count() == 3
    assert await store.count(user_id="u1") == 2
    assert await store.count(user_id="u2") == 1
    assert await store.count(user_id="nobody") == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/api_gateway && python -m pytest ../../tests/unit/conversation/test_session_store.py::test_count_matches_list_and_filters_by_user -v`
Expected: FAIL with `AttributeError: 'SessionStore' object has no attribute 'count'`

- [ ] **Step 3: Write minimal implementation**

In `apps/api_gateway/app/services/history/store.py`, insert immediately after the `list()` method's closing `return out` (currently line 142, right before `async def append_message`):

```python
    async def count(
        self, profile_id: str | None = None, user_id: str | None = None,
        source: str | None = None, client_id: str | None = None,
    ) -> int:
        async with db_session() as s:
            q = select(func.count()).select_from(ChatSession)
            if profile_id is not None:
                q = q.where(ChatSession.profile_id == profile_id)
            if user_id is not None:
                q = q.where(ChatSession.user_id == user_id)
            if source is not None:
                q = q.where(ChatSession.source == source)
            if client_id is not None:
                q = q.where(ChatSession.client_id == client_id)
            return (await s.execute(q)).scalar_one()
```

`func` and `select` are already imported at the top of this file; `ChatSession` too — no new imports needed.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/api_gateway && python -m pytest ../../tests/unit/conversation/test_session_store.py -v`
Expected: all PASS, including the new test.

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/history/store.py tests/unit/conversation/test_session_store.py
git commit --author="lugondev <lugondev@gmail.com>" -m "feat(sessions): add SessionStore.count()"
```

---

### Task 2: `GET /v1/stats/home` backend endpoint

**Files:**
- Create: `apps/api_gateway/app/api/routes/stats.py`
- Modify: `apps/api_gateway/app/main.py` (import + `include_router`)
- Modify: `apps/api_gateway/app/core/auth_guard.py` (classify `/v1/stats`)
- Test: `tests/unit/stats/test_stats_routes.py` (new file, new dir — first test in this domain)

**Interfaces:**
- Consumes: `session_store.count(user_id=...)` from Task 1; `profile_visible(profile, caller_id)` from `app.services.profile_visibility`; `device_store.list_all()` / `device_store.list_for_user(user_id)` from `app.services.auth.devices`; `profile_store.list()` from `app.services.profiles.store`; `current_user_id`, `current_role`, `scope_user_id` from `app.core.actor`.
- Produces: `GET /v1/stats/home` → `{"success": true, "data": {"profiles": {"count": int}, "devices": {"count": int, "active_recent": int}, "sessions": {"count": int}}}`. Task 3's `home.js` fetches this exact shape.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/stats/__init__.py` (empty file — matches the other per-domain test dirs, e.g. `tests/unit/usage/__init__.py`; check it exists there first with `ls tests/unit/usage/__init__.py` and mirror whatever you find — if usage has no `__init__.py`, skip creating one here too).

Create `tests/unit/stats/test_stats_routes.py`:

```python
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.auth.devices import device_store
from app.services.auth.users import user_store
from app.services.history.store import session_store
from app.services.profiles.models import Profile
from app.services.profiles.store import profile_store


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _signup_login(client, username: str, role: str = "user") -> str:
    client.post("/api/auth/signup", json={"username": username, "password": "pw"})
    user = asyncio.run(user_store.get_by_username(username))
    if role == "admin":
        asyncio.run(user_store.set_fields(user.id, role="admin"))
    client.post("/api/auth/login", json={"username": username, "password": "pw"})
    return user.id


def test_regular_user_sees_only_their_own_counts(client, _with_password):
    me_id = _signup_login(client, "toan", role="user")
    other_id = _signup_login(client, "khoa", role="user")

    profile_store.upsert(Profile(name="toan-profile", owner_id=me_id))
    profile_store.upsert(Profile(name="khoa-profile", owner_id=other_id))
    profile_store.upsert(Profile(name="template", owner_id=None))

    asyncio.run(device_store.create(me_id, "my-esp32", "serial-a"))
    asyncio.run(device_store.create(other_id, "their-esp32", "serial-b"))

    asyncio.run(session_store.create("s-mine", user_id=me_id))
    asyncio.run(session_store.create("s-theirs", user_id=other_id))

    # log back in as "toan" -- the last _signup_login call above left the
    # session logged in as "khoa"
    client.post("/api/auth/login", json={"username": "toan", "password": "pw"})

    resp = client.get("/v1/stats/home")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["profiles"]["count"] == 2  # their own + the template
    assert data["devices"]["count"] == 1
    assert data["sessions"]["count"] == 1


def test_admin_sees_global_counts(client, _with_password):
    admin_id = _signup_login(client, "root", role="admin")
    other_id = _signup_login(client, "user1", role="user")
    client.post("/api/auth/login", json={"username": "root", "password": "pw"})

    asyncio.run(device_store.create(other_id, "device-1", "serial-x"))
    asyncio.run(session_store.create("s-x", user_id=other_id))

    resp = client.get("/v1/stats/home")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["devices"]["count"] >= 1
    assert data["sessions"]["count"] >= 1


def test_device_active_recent_reflects_last_seen(client, _with_password):
    me_id = _signup_login(client, "pat", role="user")

    device, _token = asyncio.run(device_store.create(me_id, "seen", "serial-seen"))
    asyncio.run(device_store.create(me_id, "unseen", "serial-unseen"))
    asyncio.run(device_store.touch_last_seen(device["id"]))

    resp = client.get("/v1/stats/home")
    assert resp.status_code == 200
    data = resp.json()["data"]["devices"]
    assert data["count"] == 2
    assert data["active_recent"] == 1


def test_login_required(client, _with_password):
    resp = client.get("/v1/stats/home")
    assert resp.status_code in (401, 403)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/api_gateway && python -m pytest ../../tests/unit/stats/test_stats_routes.py -v`
Expected: FAIL — `404` (no such route) or import error, since `app/api/routes/stats.py` doesn't exist yet.

- [ ] **Step 3: Write the route**

Create `apps/api_gateway/app/api/routes/stats.py`:

```python
"""Role-scoped counts for the admin console's Home tab.

Everything else Home shows (model registry, active models, system health,
admin usage totals) is read straight from existing admin-only endpoints by
the frontend -- this route only supplies the three totals nothing else
already exposes: profiles, devices, sessions."""

from datetime import datetime, timezone

from fastapi import APIRouter, Request

from app.core.actor import current_role, current_user_id, scope_user_id
from app.services.auth.devices import device_store
from app.services.history.store import session_store
from app.services.profile_visibility import profile_visible
from app.services.profiles.store import profile_store

router = APIRouter(prefix="/v1/stats", tags=["stats"])

# `last_seen_at` is only touched once per new WS handshake (see
# auth_guard.py), never on a heartbeat during a long-lived connection -- so
# this is a "recently active" proxy, not a live-online signal. The UI must
# label it accordingly and never call it "online".
_ACTIVE_RECENT_MINUTES = 30


def _is_recently_active(last_seen_at: str | None) -> bool:
    if not last_seen_at:
        return False
    seen = datetime.fromisoformat(last_seen_at)
    return (datetime.now(timezone.utc) - seen).total_seconds() <= _ACTIVE_RECENT_MINUTES * 60


@router.get("/home")
async def home_stats(request: Request) -> dict:
    user_id = current_user_id(request)
    role = current_role(request)

    profiles = profile_store.list()
    profile_count = sum(1 for p in profiles.values() if profile_visible(p, user_id))

    devices = (
        await device_store.list_all()
        if role == "admin"
        else await device_store.list_for_user(user_id or "")
    )
    live_devices = [d for d in devices if not d["revoked"]]
    active_recent = sum(1 for d in live_devices if _is_recently_active(d["last_seen_at"]))

    session_count = await session_store.count(user_id=scope_user_id(request))

    return {
        "success": True,
        "data": {
            "profiles": {"count": profile_count},
            "devices": {"count": len(live_devices), "active_recent": active_recent},
            "sessions": {"count": session_count},
        },
    }
```

- [ ] **Step 4: Wire the router into `main.py`**

In `apps/api_gateway/app/main.py`, add the import alphabetically between the existing `sessions` and `stt` imports (currently lines 27-28):

```python
from app.api.routes.sessions import router as sessions_router
from app.api.routes.stats import router as stats_router
from app.api.routes.stt import router as stt_router
```

Then add `app.include_router(stats_router)` right after `app.include_router(sessions_router)` in the `include_router` block (currently line 308).

- [ ] **Step 5: Classify `/v1/stats` in the auth guard**

In `apps/api_gateway/app/core/auth_guard.py`, add `"/v1/stats"` to the `_USER_PREFIXES` tuple, after the existing `"/v1/sessions",` entry:

```python
_USER_PREFIXES = (
    "/ui",
    "/static/",
    "/v1/events",
    "/v1/conversation",
    "/v1/profiles",
    "/v1/mcp",
    "/v1/stt",
    "/v1/tts",
    "/v1/sessions",
    "/v1/stats",
)
```

This is reachable by any logged-in user (admin or not) — the route itself branches on role, matching how `/v1/sessions` already works.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd apps/api_gateway && python -m pytest ../../tests/unit/stats/test_stats_routes.py ../../tests/unit/http/test_auth_guard_route_coverage.py -v`
Expected: all PASS. The route-coverage test is the anti-omission harness mentioned in the Global Constraints — it now passes because `/v1/stats` is classified.

- [ ] **Step 7: Commit**

```bash
git add apps/api_gateway/app/api/routes/stats.py apps/api_gateway/app/main.py apps/api_gateway/app/core/auth_guard.py tests/unit/stats/
git commit --author="lugondev <lugondev@gmail.com>" -m "feat(api): add GET /v1/stats/home for role-scoped profile/device/session counts"
```

---

### Task 3: Home tab scaffolding + role-agnostic widgets

**Files:**
- Modify: `apps/api_gateway/app/static/index.html` (nav item, new `section-home`, flip default-active tab)
- Modify: `apps/api_gateway/app/static/js/sidebar-nav.js` (wire `loadHome`, dispatch on click)
- Modify: `apps/api_gateway/app/static/js/main.js` (eager-load `loadHome()` at startup)
- Create: `apps/api_gateway/app/static/js/home.js`

**Interfaces:**
- Consumes: `GET /v1/stats/home` (Task 2), `GET /v1/usage/me`, `GET /v1/usage/summary?group_by=kind`, `GET /v1/quotas` (all pre-existing), `fetchAuthStatus()` from `./session.js`, `el`/`escapeHtml` from `./helpers.js`.
- Produces: `export async function loadHome()` from `home.js` — Task 4 adds more functions to this same file and extends `loadHome()`'s body to call them for admins.

- [ ] **Step 1: Add the Home nav item and flip the default-active tab in `index.html`**

Find this block (currently lines 56-62):

```html
          <ul class="nav-list">
            <li>
              <button class="nav-item active" data-section="conversation">
                <span class="nav-icon">◈</span>
                <span class="nav-label">Conversation</span>
              </button>
            </li>
```

Replace with:

```html
          <ul class="nav-list">
            <li>
              <button class="nav-item active" data-section="home">
                <span class="nav-icon">&#8962;</span>
                <span class="nav-label">Home</span>
              </button>
            </li>
            <li>
              <button class="nav-item" data-section="conversation">
                <span class="nav-icon">◈</span>
                <span class="nav-label">Conversation</span>
              </button>
            </li>
```

Then find the Conversation section's opening tag (currently line 151):

```html
          <div class="section active" id="section-conversation">
```

Replace with (drop `active`):

```html
          <div class="section" id="section-conversation">
```

Then, immediately BEFORE that same `<div class="section" id="section-conversation">` line, insert the new Home section:

```html
          <!-- ============================== HOME (dashboard) ============================== -->
          <div class="section active" id="section-home">
            <section class="card">
              <div class="card-head">
                <h2>Overview</h2>
              </div>
              <div id="home-overview" class="status-grid">
                <p class="hint">Loading&#8230;</p>
              </div>
            </section>

            <section class="card">
              <div class="card-head">
                <h2>Usage this month</h2>
              </div>
              <div id="home-usage" class="status-grid">
                <p class="hint">Loading&#8230;</p>
              </div>
            </section>

            <section class="card admin-only">
              <div class="card-head">
                <h2>System health</h2>
              </div>
              <div id="home-health" class="status-grid"></div>
            </section>

            <section class="card admin-only">
              <div class="card-head">
                <h2>Active models</h2>
              </div>
              <div id="home-active-models" class="status-grid"></div>
            </section>

            <section class="card admin-only">
              <div class="card-head">
                <h2>Model registry</h2>
                <button id="home-registry-link" class="ghost mini">Open Model Registry</button>
              </div>
              <div id="home-registry" class="status-grid"></div>
            </section>
          </div>

```

Note: `initSidebar()` already un-hides *every* `.admin-only` element for an admin caller (it's a generic `document.querySelectorAll(".admin-only")` sweep, not scoped to nav items) — putting the class directly on these three `<section>`s is enough; no new JS is needed to show/hide them.

- [ ] **Step 2: Create `home.js` with the role-agnostic widgets**

Create `apps/api_gateway/app/static/js/home.js`:

```js
import { el, escapeHtml } from "./helpers.js";
import { fetchAuthStatus } from "./session.js";

function _tile(label, value, ok) {
  const cls = ok === undefined ? "" : ok ? "ok" : "warn";
  return `<div class="stat ${cls}"><span>${label}</span><strong>${value}</strong></div>`;
}

async function _loadOverview() {
  const host = el("home-overview");
  if (!host) return;
  try {
    const body = await (await fetch("/v1/stats/home")).json();
    if (!body.success) throw new Error("failed to load stats");
    const d = body.data;
    host.innerHTML =
      _tile("Profiles", d.profiles.count) +
      _tile("Devices", `${d.devices.count} (${d.devices.active_recent} active recently)`) +
      _tile("Sessions", d.sessions.count);
  } catch (error) {
    host.innerHTML = _tile("Overview", "error", false);
  }
}

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

async function _loadUsageForUser() {
  const host = el("home-usage");
  if (!host) return;
  try {
    const body = await (await fetch("/v1/usage/me")).json();
    if (!body.success) throw new Error("failed to load usage");
    const rows = body.data || [];
    const requests = rows.reduce((s, r) => s + Number(r.count || 0), 0);
    const cost = rows.reduce((s, r) => s + Number(r.cost_usd || 0), 0);
    host.innerHTML =
      _tile("Requests (all time)", requests) +
      _tile("Cost (all time)", `$${cost.toFixed(4)}`) +
      _renderLimits(body.limits);
  } catch (error) {
    host.innerHTML = _tile("Usage", "error", false);
  }
}

export async function loadHome() {
  await Promise.all([_loadOverview(), _loadUsageForAdminOrUser()]);
}

async function _loadUsageForAdminOrUser() {
  const status = await fetchAuthStatus();
  const isAdmin = status.authenticated && status.role === "admin";
  if (!isAdmin) {
    await _loadUsageForUser();
    return;
  }
  await _loadUsageForAdmin();
}

async function _loadUsageForAdmin() {
  const host = el("home-usage");
  if (!host) return;
  try {
    const [summaryBody, quotasBody] = await Promise.all([
      (await fetch("/v1/usage/summary?group_by=kind")).json(),
      (await fetch("/v1/quotas")).json(),
    ]);
    if (!summaryBody.success) throw new Error("failed to load usage");
    const rows = summaryBody.data || [];
    const requests = rows.reduce((s, r) => s + Number(r.count || 0), 0);
    const cost = rows.reduce((s, r) => s + Number(r.cost_usd || 0), 0);
    const quotas = quotasBody.success ? quotasBody.data || [] : [];
    host.innerHTML =
      _tile("Requests (all time)", requests) +
      _tile("Cost (all time)", `$${cost.toFixed(4)}`) +
      _renderQuotaLimits(quotas);
  } catch (error) {
    host.innerHTML = _tile("Usage", "error", false);
  }
}

function _renderQuotaLimits(quotas) {
  const enabled = (quotas || []).filter((q) => q.enabled);
  if (!enabled.length) return "";
  const parts = enabled.map((q) => {
    const spent = Number(q.spend_usd || 0);
    const limit = Number(q.limit_usd || 0);
    const over = limit > 0 && spent >= limit;
    const label = `${escapeHtml(q.scope)}${q.scope_id ? ` (${escapeHtml(q.scope_id)})` : ""}`;
    return `<li class="${over ? "danger" : ""}">${label} — ${escapeHtml(q.period)}: $${spent.toFixed(4)} of $${limit.toFixed(2)}${over ? " - reached" : ""}</li>`;
  });
  return `<ul class="limit-list">${parts.join("")}</ul>`;
}
```

This intentionally leaves `_loadUsageForAdmin`/`_renderQuotaLimits` in the file even though Task 4 hasn't added the *other* admin widgets yet — usage/quota was scoped to this task because it's a role-agnostic concept (everyone gets a usage widget, admin's is just a different data source), whereas system-health/active-models/registry are genuinely admin-only sections added in Task 4.

- [ ] **Step 3: Wire `home.js` into `sidebar-nav.js`**

In `apps/api_gateway/app/static/js/sidebar-nav.js`, add the import alongside the others:

```js
import { loadHome } from "./home.js";
```

And add a dispatch line in `activateSection()`, alongside the existing `if (section === "...")` lines:

```js
  if (section === "home") loadHome();
```

- [ ] **Step 4: Eager-load Home at startup in `main.js`**

In `apps/api_gateway/app/static/js/main.js`, add the import:

```js
import { loadHome } from "./home.js";
```

And add `loadHome();` to the eager-load sequence at the bottom of the file, alongside `loadProfiles();`, `loadMcpServers();`, etc. (anywhere in that block — order doesn't matter, every call there is independent and already async).

- [ ] **Step 5: Manual smoke check (no automated test for static JS in this repo)**

Run: `cd apps/api_gateway && python -m pytest ../../tests/unit/stats -v` (confirms the backend this page depends on is still green; there's no JS test harness in this repo — the real check is the browser pass in Task 5).

Also sanity-check the edited files parse: `node --check apps/api_gateway/app/static/js/home.js && node --check apps/api_gateway/app/static/js/sidebar-nav.js && node --check apps/api_gateway/app/static/js/main.js`. Note: `node --check` only catches syntax errors, not encoding corruption (e.g. smart quotes swapped in for straight ones) — visually re-read any line you typed a `'` or `"` into before trusting this check.

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/static/index.html apps/api_gateway/app/static/js/home.js apps/api_gateway/app/static/js/sidebar-nav.js apps/api_gateway/app/static/js/main.js
git commit --author="lugondev <lugondev@gmail.com>" -m "feat(admin-ui): add Home tab with overview + usage widgets, make it the default landing tab"
```

---

### Task 4: Admin-only widgets (system health, active models, model registry)

**Files:**
- Modify: `apps/api_gateway/app/static/js/home.js` (add three functions + extend `loadHome()`)

**Interfaces:**
- Consumes: `GET /v1/system/status`, `GET /v1/models`, `GET /v1/model_registry` (all pre-existing, admin-only), the `_tile` helper and `el`/`escapeHtml` imports from Task 3.
- Produces: `loadHome()` now also populates `#home-health`, `#home-active-models`, `#home-registry` when the caller is an admin.

- [ ] **Step 1: Add the three admin widget functions**

Append to `apps/api_gateway/app/static/js/home.js`:

```js
async function _loadSystemAndModels() {
  const healthHost = el("home-health");
  const modelsHost = el("home-active-models");
  if (!healthHost || !modelsHost) return;
  try {
    const [statusBody, modelsBody] = await Promise.all([
      (await fetch("/v1/system/status")).json(),
      (await fetch("/v1/models")).json(),
    ]);
    const d = statusBody.data;
    const llm = modelsBody.data.llm;
    const sttOk = (d.stt_engines || []).some((e) => e.available);
    const ttsOk = (d.tts_engines || []).some((e) => e.available);

    healthHost.innerHTML =
      _tile("STT", sttOk ? "ready" : "not ready", sttOk) +
      _tile("TTS", ttsOk ? "ready" : "not ready", ttsOk) +
      _tile("LLM", llm.available ? "ready" : "not ready", llm.available);

    const whisperActive = d.whisper_local?.active_model || "(none)";
    const voskActive = d.vosk?.active_model_present ? "installed" : "not installed";
    modelsHost.innerHTML =
      _tile("Whisper (local STT)", escapeHtml(whisperActive)) +
      _tile("Vosk (local STT)", escapeHtml(voskActive)) +
      _tile("LLM active model", escapeHtml(llm.active || "(none)")) +
      _tile("LLM endpoint", llm.remote ? "Cloud API" : `Ollama (${llm.running ? "running" : "idle"})`);
  } catch (error) {
    healthHost.innerHTML = _tile("System health", "error", false);
    modelsHost.innerHTML = "";
  }
}

async function _loadRegistrySummary() {
  const host = el("home-registry");
  if (!host) return;
  try {
    const body = await (await fetch("/v1/model_registry")).json();
    if (!body.success) throw new Error("failed to load registry");
    const entries = body.data || [];
    const byKind = {};
    entries.forEach((e) => {
      byKind[e.kind] = (byKind[e.kind] || 0) + 1;
    });
    const kindTiles = Object.keys(byKind)
      .sort()
      .map((k) => _tile(k.toUpperCase(), byKind[k]))
      .join("");
    host.innerHTML = _tile("Total entries", entries.length) + kindTiles;
  } catch (error) {
    host.innerHTML = _tile("Model registry", "error", false);
  }
}

if (el("home-registry-link")) {
  el("home-registry-link").addEventListener("click", () => {
    document.querySelector('[data-section="model-registry"]')?.click();
  });
}
```

- [ ] **Step 2: Wire them into `loadHome()`/`_loadUsageForAdminOrUser()`**

In `home.js`, change the `_loadUsageForAdminOrUser` function (from Task 3) to also trigger the three new admin widgets:

```js
async function _loadUsageForAdminOrUser() {
  const status = await fetchAuthStatus();
  const isAdmin = status.authenticated && status.role === "admin";
  if (!isAdmin) {
    await _loadUsageForUser();
    return;
  }
  await Promise.all([_loadUsageForAdmin(), _loadSystemAndModels(), _loadRegistrySummary()]);
}
```

- [ ] **Step 3: Syntax check**

Run: `node --check apps/api_gateway/app/static/js/home.js`
Then visually re-read the file with the Read tool (not `cat`) end to end — this repo's shell has a known encoding quirk that can silently corrupt smart quotes / dashes on write, and `node --check` will not catch that.

- [ ] **Step 4: Commit**

```bash
git add apps/api_gateway/app/static/js/home.js
git commit --author="lugondev <lugondev@gmail.com>" -m "feat(admin-ui): add admin-only system health, active models, and model registry widgets to Home"
```

---

### Task 5: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full scoped test suite for everything touched**

Run: `cd apps/api_gateway && python -m pytest ../../tests/unit/conversation/test_session_store.py ../../tests/unit/stats ../../tests/unit/http/test_auth_guard_route_coverage.py ../../tests/unit/http/test_auth_guard_path_classification.py ../../tests/unit/http/test_auth_guard_default_deny.py -v`
Expected: all PASS.

- [ ] **Step 2: Browser verification — admin**

Use the `run` skill (or start the dev server manually per this repo's usual local-run instructions) and, in a browser, log in as an admin user:
- Confirm the app lands on the **Home** tab by default (no `?tab=` in the URL).
- Confirm Overview shows non-error tiles for Profiles/Devices/Sessions.
- Confirm Usage this month, System health, Active models, and Model registry sections are all visible and populated (not "error" tiles).
- Click "Open Model Registry" and confirm it switches to the Model Registry tab.
- Click the Conversation nav item and back to Home; confirm Home's widgets still render correctly on re-entry (no stale/empty state).

- [ ] **Step 3: Browser verification — regular user**

Log in (or sign up) as a non-admin user:
- Confirm Home still loads by default, showing Overview + Usage tiles scoped to that user (not global counts — verify by comparing against what that user actually owns).
- Confirm System health / Active models / Model registry sections are **absent** (not just empty) — inspect the DOM if needed to confirm they're `display: none` via the `admin-only` class staying in place.

- [ ] **Step 4: If this repo's project conventions require running the full test suite before considering work "done" (check CLAUDE.md / memory for this), run it now** — otherwise the scoped run in Step 1 plus the browser checks above are the completion bar for this plan.

- [ ] **Step 5: Report results**

Summarize: which of Steps 1-3 passed, any deviations found, and whether the "active_recent" device caveat (30-min recently-active window vs. true live-online) needs a follow-up decision from the user before this is considered fully done.
