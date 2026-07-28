# Critical Authorization Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the five Critical authorization holes found in the 2026-07-28 audit, and flip the auth middleware from default-allow to default-deny so a future router that forgets to declare a prefix fails closed instead of open.

**Architecture:** Six independent fixes. Task 1 (default-deny) is the riskiest and lands first because Task 2 depends on it. Tasks 3-6 are self-contained route/validation changes.

**Tech Stack:** Python 3.12, FastAPI, Starlette middleware, pydantic v2, pytest + pytest-asyncio, httpx MockTransport.

## Global Constraints

- Run tests with `.venv/bin/pytest` from the repo root (a symlinked venv exists in this worktree).
- **Test hermeticity, learned the hard way in this repo:** a test must never depend on an optional pip extra being installed (`vieneu`/`omnivoice` are under optional extras, not `dev`) or on a real model file under the gitignored `models/`. Neutralize such gates with `monkeypatch` rather than relying on machine state.
- `tests/conftest.py` has autouse fixtures (`_hermetic`, `_hermetic_engine_health`) that patch cross-cutting concerns in one place — follow that pattern rather than copying blocks into many test files.
- Expected pre-existing failure, do NOT try to fix: `tests/integration/test_stt_ws.py::test_ws_stream_partial_then_final_then_done` (fails identically on untouched main). A single `StarletteDeprecationWarning` from `fastapi.testclient` is likewise pre-existing repo-wide.
- Baseline before this plan: `.venv/bin/pytest tests/unit tests/integration -q` → **1540 passed, 1 failed** (the one above).
- Commit as the repo default identity; do not override.

**Audit source:** the five Criticals plus the systemic default-allow root cause, all controller-verified against the code.

---

## File Structure

**Modify:**
- `apps/api_gateway/app/core/auth_guard.py` — default-deny + segment-boundary matching + explicit public allowlist.
- `apps/api_gateway/app/api/routes/events.py` — owner scoping on both SSE channels.
- `apps/api_gateway/app/api/routes/conversation.py` — admin-gate the LLM config routes; owner-check `session_id` on chat and WS resume.
- `apps/api_gateway/app/schemas/tts.py` + `apps/api_gateway/app/services/artifacts.py` — constrain `ref_audio_path` to the artifacts directory.
- `apps/api_gateway/app/api/routes/mcp.py` — admin-only writes.
- `apps/api_gateway/app/services/mcp/models.py` (or a new validator module) — block private/loopback/link-local destinations.

**Create:**
- `tests/unit/test_auth_guard_default_deny.py`
- `tests/unit/test_events_scoping.py`
- `tests/unit/test_conversation_authz.py`
- `tests/unit/test_ref_audio_path_containment.py`
- `tests/unit/test_mcp_ssrf.py`

---

### Task 1: Auth middleware — default-deny + segment boundaries

**Files:**
- Modify: `apps/api_gateway/app/core/auth_guard.py`
- Test: `tests/unit/test_auth_guard_default_deny.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: a `_PUBLIC_PATHS`/`_PUBLIC_PREFIXES` allowlist other tasks rely on; `_matches` gains segment-boundary semantics.

**Background — the complete mounted-surface inventory (controller-verified, use this as your checklist):**

Routers and their real path prefixes (`main.py:256-278`):
| Router | Real paths | Intended access |
|---|---|---|
| health | `/health` | public (liveness) |
| auth | `/api/auth/*` | public (login/signup) |
| users | `/v1/users` | admin |
| stt | `/v1/stt` | user |
| tts | `/v1/tts` | user |
| tts_profiles | `/v1/tts/profiles` | user |
| events | `/v1/events` | **user (currently unguarded — this task fixes it)** |
| conversation | `/v1/conversation` | user |
| devices | `/v1/devices` | admin, except `/mine*`, `/pair/claim` (user) and `/pair/init`, `/pair/status` (public) |
| system | `/v1/system/*`, `/v1/models/*` (router prefix is `/v1`) | admin |
| livehost | `/v1/livehost` | user |
| lugo | `/v1/lugo` | user (WS auth internal) |
| recommend | `/v1/models/*` (router prefix is `/v1`) | admin |
| ui | `/ui` (no router prefix) | user |
| agents_docs | `/agents-docs` (no router prefix) | **currently unguarded** |
| profiles | `/v1/profiles` | user |
| mcp | `/v1/mcp` | user read, admin write (Task 6) |
| sessions | `/v1/sessions` | user |
| memories | `/v1/profiles/{name}/memories` | user |
| model_registry | `/v1/model_registry` | admin, except `/options`, `/defaults` (user) |
| providers | `/v1/providers` | admin |
| quotas | `/v1/quotas` | admin |
| usage | `/v1/usage` | admin, except `/me` (user) |

Non-router surface:
- `app.mount("/static", ...)` (`main.py:280`) — currently `/static/` is in `_USER_PREFIXES` with a `_STATIC_ALLOWLIST` carve-out for the login page assets (`/static/login.html`, `/static/js/auth.js`, `/static/styles.css`, `/static/brand/favicon.svg`, `/static/brand/logo-mark-light.svg`).
- `app.mount("/artifacts", ...)` (`main.py:282`) — **currently unguarded.**
- `@app.get("/")` (`main.py:285`) — returns `{"service", "env"}` only.
- FastAPI auto-routes `/docs`, `/redoc`, `/openapi.json` — **currently unguarded.**

**Decisions already made (do not relitigate):**
- Default-deny: anything not explicitly listed requires at least a logged-in user.
- `/artifacts` becomes user-level (its ids are unguessable uuid4, but it must not be anonymous).
- `/agents-docs` becomes **admin**-level (it is the internal runbook + API map).
- `/docs`, `/redoc`, `/openapi.json` become **admin**-level.
- `/` and `/health` stay public.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_auth_guard_default_deny.py`:

```python
"""The guard must fail CLOSED: a path nobody classified requires auth.

Before this, AuthGuardMiddleware ended in `return await call_next(request)`,
so /v1/events, /agents-docs, /artifacts and /openapi.json were all reachable
with no credentials purely because nobody had added them to a prefix tuple.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.mark.parametrize("path", ["/", "/health"])
def test_public_paths_need_no_auth(client, path):
    assert client.get(path).status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/v1/events/sessions/abc",
        "/v1/events/jobs/abc",
        "/artifacts/deadbeef.wav",
        "/agents-docs",
        "/openapi.json",
        "/docs",
        "/some/router/nobody/classified",
    ],
)
def test_previously_open_paths_now_require_auth(client, path):
    """Anonymous callers must be rejected (401/403), never served (2xx)."""
    resp = client.get(path)
    assert resp.status_code in (401, 403), f"{path} returned {resp.status_code}"


def test_login_page_assets_stay_public(client):
    """The login page must load before anyone has a session."""
    for path in ("/static/login.html", "/static/js/auth.js", "/static/styles.css"):
        assert client.get(path).status_code == 200, path


def test_pairing_handshake_stays_public(client):
    """A device has no login; pair/init and pair/status must stay anonymous."""
    resp = client.post("/v1/devices/pair/init", json={"serial": "SN1"})
    assert resp.status_code != 401


def test_segment_boundary_prevents_prefix_smuggling():
    """`/v1/usage/me` is a user carve-out inside the admin `/v1/usage` prefix.
    A raw startswith would also admit `/v1/usage/metrics` as user-level."""
    from app.core.auth_guard import _matches

    assert _matches("/v1/usage/me", ("/v1/usage/me",)) is True
    assert _matches("/v1/usage/me/detail", ("/v1/usage/me",)) is True
    assert _matches("/v1/usage/metrics", ("/v1/usage/me",)) is False
    assert _matches("/v1/model_registry/optionsets", ("/v1/model_registry/options",)) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_auth_guard_default_deny.py -v`
Expected: FAIL — the previously-open paths return 200, and `_matches` admits `/v1/usage/metrics`.

- [ ] **Step 3: Write the implementation**

In `apps/api_gateway/app/core/auth_guard.py`:

1. Change `_matches` to require a segment boundary:

```python
def _matches(path: str, prefixes: tuple[str, ...]) -> bool:
    """Segment-aware prefix match.

    A raw startswith would make `/v1/usage/metrics` match the `/v1/usage/me`
    user carve-out and silently escape the admin rule it sits inside.
    """
    return any(path == prefix or path.startswith(prefix.rstrip("/") + "/") for prefix in prefixes)
```

Note `/static/` is stored with a trailing slash today — `rstrip("/")` keeps it working.

2. Add an explicit public list next to the existing tuples:

```python
# Reachable with no credentials at all. Everything NOT classified here or in
# the user/admin tuples below is denied by default -- see dispatch().
_PUBLIC_PATHS = frozenset({"/", "/health"})
```

3. Add `/v1/events` to `_USER_PREFIXES` and `/artifacts` to `_USER_PREFIXES`; add `/agents-docs`, `/docs`, `/redoc`, `/openapi.json` to `_ADMIN_PREFIXES`.

4. Replace the trailing `return await call_next(request)` in `dispatch` with a default-deny, keeping the existing user/admin branches above it unchanged:

```python
        # Default-DENY. Anything not classified above is treated as at least
        # user-level rather than public, so a newly mounted router that nobody
        # remembered to classify fails closed instead of being served to the
        # internet (which is exactly how /v1/events, /agents-docs, /artifacts
        # and /openapi.json ended up unauthenticated).
        if not user_id:
            return self._unauthenticated(request)
        return await call_next(request)
```

5. Early in `dispatch`, before the auth work, allow the public set:

```python
        if path in _PUBLIC_PATHS:
            return await call_next(request)
```

Keep the existing `_STATIC_ALLOWLIST` / `/api/auth` / `_NO_AUTH_PREFIXES` carve-out line exactly as it is.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_auth_guard_default_deny.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite — this task can break anything**

Run: `.venv/bin/pytest tests/unit tests/integration -q`
Expected: 1540 passed, 1 pre-existing failure. **Any new failure is a real signal that a legitimate path just got denied** — investigate and classify that path correctly rather than weakening the default-deny.

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/core/auth_guard.py tests/unit/test_auth_guard_default_deny.py
git commit -m "fix(auth): default-deny unclassified paths and match on segment boundaries"
```

---

### Task 2: Scope `/v1/events` channels to their owner

**Files:**
- Modify: `apps/api_gateway/app/api/routes/events.py`
- Test: `tests/unit/test_events_scoping.py`

**Interfaces:**
- Consumes: Task 1's guard (these routes are now authenticated).
- Produces: nothing downstream.

**Background:** Task 1 makes these routes require a login, but any logged-in user can still subscribe to any session's channel. `stream_session_events(session_id)` must 404 unless the caller owns that session; `sessions.py:58-64` is the reference pattern (`session_store.get()`, compare `sess["user_id"]` to `_scope_user_id(request)`, 404 otherwise — admins have `scope is None` and see everything). Read both `sessions.py`'s `_scope_user_id` and `get_session` before writing this.

Job channels (`/jobs/{job_id}`) have no owner recorded today. Record the creating user when the job is created (`tts.py`'s stream-job creation) and check it here; if that proves impractical within this task, 404 job channels for non-admins and say so in your report rather than leaving them open to all users.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_events_scoping.py`:

```python
"""A logged-in user must not be able to subscribe to another user's live
transcript stream. Mirrors the ownership rule sessions.py already enforces."""

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_session_channel_404s_for_non_owner(client, monkeypatch):
    """Owner is 'alice'; caller is 'bob' -> 404, not a live stream."""
    async def fake_get(session_id):
        return {"id": session_id, "user_id": "alice"}

    monkeypatch.setattr("app.api.routes.events.session_store.get", fake_get, raising=False)
    monkeypatch.setattr("app.api.routes.events._scope_user_id", lambda request: "bob", raising=False)

    resp = client.get("/v1/events/sessions/s-alice")
    assert resp.status_code == 404


def test_session_channel_allows_owner(client, monkeypatch):
    async def fake_get(session_id):
        return {"id": session_id, "user_id": "alice"}

    monkeypatch.setattr("app.api.routes.events.session_store.get", fake_get, raising=False)
    monkeypatch.setattr("app.api.routes.events._scope_user_id", lambda request: "alice", raising=False)

    with client.stream("GET", "/v1/events/sessions/s-alice") as resp:
        assert resp.status_code == 200


def test_session_channel_404s_for_unknown_session(client, monkeypatch):
    async def fake_get(session_id):
        return None

    monkeypatch.setattr("app.api.routes.events.session_store.get", fake_get, raising=False)
    monkeypatch.setattr("app.api.routes.events._scope_user_id", lambda request: "bob", raising=False)

    assert client.get("/v1/events/sessions/ghost").status_code == 404
```

Adjust the monkeypatch targets to whatever names your implementation actually imports into `events.py` — the assertions are what matter, not the exact patch path.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_events_scoping.py -v`
Expected: FAIL — no ownership check exists yet.

- [ ] **Step 3: Write the implementation**

Give both routes a `Request` parameter, import the same scoping helper `sessions.py` uses (or replicate its three lines), resolve the session, and `raise HTTPException(404, ...)` unless the caller owns it or is an admin. Subscribe to the event bus only after that check passes.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_events_scoping.py -v`
Expected: PASS

- [ ] **Step 5: Check the one real consumer still works**

`apps/api_gateway/app/static/js/tts-stream.js:70` opens `new EventSource('/v1/events/jobs/${jobId}')`. `EventSource` sends cookies for same-origin requests, so the session cookie carries — but it cannot set an `Authorization` header. Confirm your job-channel decision does not break that page, and state in your report how you verified it.

Run: `.venv/bin/pytest tests/unit tests/integration -q`
Expected: 1540 passed, 1 pre-existing failure.

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/api/routes/events.py tests/unit/test_events_scoping.py
git commit -m "fix(events): scope SSE channels to their owning user"
```

---

### Task 3: Admin-gate the conversation LLM config routes

**Files:**
- Modify: `apps/api_gateway/app/api/routes/conversation.py` (the three `/llm` routes at ~lines 86-102)
- Test: `tests/unit/test_conversation_authz.py`

**Interfaces:**
- Consumes: the repo's existing admin-check helper (find how `system.py` or `users.py` enforces admin, and follow it).
- Produces: nothing downstream.

**Background:** `GET /v1/conversation/llm`, `POST /v1/conversation/llm` and `POST /v1/conversation/llm/reset` sit under the `/v1/conversation` user prefix but write straight into the Model Registry's default LLM row via `set_active_llm_config` (`services/conversation/responder.py`). Any logged-in user can repoint every other user's LLM at an endpoint they control — exfiltrating all prompts, system prompts and injected memories — or drop the whole server to the echo responder with one request.

The only client is `apps/api_gateway/app/static/js/model-recommender.js:216,244,265`, which is an admin tool, so admin-gating does not break a user-facing flow. Verify that claim yourself before committing.

Prefer an inline admin check inside the three handlers over moving the routes to a new prefix — moving them would change the public API path and break `model-recommender.js`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_conversation_authz.py`:

```python
"""The /llm routes mutate a SERVER-WIDE registry row. They must be admin-only
even though they live under the /v1/conversation user prefix."""

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def _as_user(client, role: str):
    """Give the TestClient a session cookie carrying the given role.
    Match however the existing tests in this repo fake a logged-in session --
    find them first (grep for 'session' in tests/unit) and reuse that helper."""
    raise NotImplementedError("replace with this repo's existing session fixture")


def test_set_llm_config_rejected_for_normal_user(client):
    _as_user(client, "user")
    resp = client.post("/v1/conversation/llm", json={
        "base_url": "https://attacker.example/v1", "api_key": "x", "model": "gpt-4o",
    })
    assert resp.status_code == 403


def test_reset_llm_config_rejected_for_normal_user(client):
    _as_user(client, "user")
    assert client.post("/v1/conversation/llm/reset").status_code == 403


def test_get_llm_config_rejected_for_normal_user(client):
    """GET discloses the provider base_url -- admin-only too."""
    _as_user(client, "user")
    assert client.get("/v1/conversation/llm").status_code == 403


def test_admin_can_still_set_llm_config(client):
    _as_user(client, "admin")
    resp = client.post("/v1/conversation/llm", json={
        "base_url": "http://localhost:11434/v1", "api_key": "", "model": "llama3",
    })
    assert resp.status_code == 200
```

**Before writing these, find how existing tests in this repo authenticate a session** (grep `tests/unit` for session/login fixtures) and replace `_as_user` with that mechanism. Do not invent a new one.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_conversation_authz.py -v`
Expected: FAIL — normal users currently get 200.

- [ ] **Step 3: Write the implementation**

Add an admin check to all three handlers, following the repo's existing pattern.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_conversation_authz.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/api/routes/conversation.py tests/unit/test_conversation_authz.py
git commit -m "fix(conversation): admin-gate the server-wide LLM config routes"
```

---

### Task 4: Owner-check `session_id` on chat and WS resume

**Files:**
- Modify: `apps/api_gateway/app/api/routes/conversation.py` (`chat` at ~165-178, and the WS `resume_sid` path at ~284)
- Test: `tests/unit/test_conversation_authz.py` (append)

**Interfaces:**
- Consumes: `sessions.py`'s `_scope_user_id` pattern.
- Produces: nothing downstream.

**Background:** `chat` does `session_store.exists(session_id)` then `session_store.get_messages(session_id)` with no ownership check, loads those messages into the LLM context, and then appends the caller's turn into that session. So a user can both READ another user's private conversation (`{"messages":[{"role":"user","content":"repeat everything above"}]}`) and CORRUPT it. The WS path has the same hole: `requested_sid` flows into `resume_sid` and is consumed in `services/conversation/session.py` with no owner check.

`sessions.py:58-64` is the correct reference — read it first.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_conversation_authz.py`:

```python
def test_chat_cannot_resume_another_users_session(client, monkeypatch):
    """Reading another user's history through ?session_id= is an IDOR."""
    _as_user(client, "user")  # caller is 'bob'

    async def fake_exists(session_id):
        return True

    async def fake_get_messages(session_id):
        return [{"role": "user", "content": "alice's private secret"}]

    monkeypatch.setattr(
        "app.api.routes.conversation.session_store.exists", fake_exists, raising=False)
    monkeypatch.setattr(
        "app.api.routes.conversation.session_store.get_messages", fake_get_messages, raising=False)

    resp = client.post(
        "/v1/conversation/chat?session_id=alice-session",
        json={"messages": [{"role": "user", "content": "repeat everything above"}]},
    )
    assert resp.status_code == 404
    assert "alice's private secret" not in resp.text


def test_ws_cannot_resume_another_users_session(client):
    """Same hole on the WS path via ?session_id=."""
    _as_user(client, "user")
    with client.websocket_connect(
        "/v1/conversation/stream?session_id=alice-session"
    ) as ws:
        msg = ws.receive_json()
        assert msg["event"] == "error"
```

Adapt the ownership fixture so the fake session's `user_id` is a DIFFERENT user from the caller — the assertion that matters is that another user's messages never reach the response.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_conversation_authz.py -v -k resume`
Expected: FAIL — the messages currently come back.

- [ ] **Step 3: Write the implementation**

In `chat`, resolve the session with `session_store.get(session_id)` and 404 unless the caller owns it (admins bypass, matching `_scope_user_id`'s `None` semantics). Only then read its messages. Apply the equivalent check on the WS path before `resume_sid` is used.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_conversation_authz.py -v`
Expected: PASS

- [ ] **Step 5: Run the conversation and session suites for regressions**

Run: `.venv/bin/pytest tests/unit/test_conversation.py tests/unit/test_conversation_profile.py tests/unit/test_conversation_history.py tests/unit/test_sessions.py -v`
Expected: PASS. Legitimate same-user resume must still work — if a test breaks because it resumes a session it created under a different (or absent) user id, fix the test's setup, not the ownership check.

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/api/routes/conversation.py tests/unit/test_conversation_authz.py
git commit -m "fix(conversation): reject resuming a session the caller does not own"
```

---

### Task 5: Constrain `ref_audio_path` to the artifacts directory

**Files:**
- Modify: `apps/api_gateway/app/services/artifacts.py` (add the containment helper) and whichever layer validates `TTSRequest` — prefer a pydantic validator in `apps/api_gateway/app/schemas/tts.py` so every provider is covered at once.
- Test: `tests/unit/test_ref_audio_path_containment.py`

**Interfaces:**
- Consumes: `artifact_store.base_dir`.
- Produces: a reusable containment check.

**Background:** `TTSRequest.ref_audio_path` (`schemas/tts.py:15`) is an unvalidated `str | None` that six providers feed straight to a filesystem read — `http_tts_provider.py:168` does `Path(payload.ref_audio_path).read_bytes()` and base64s the result into an outbound HTTP request. Any logged-in user can read `/app/.env` or hang a worker on `/dev/zero`.

**Decision already made (do not relitigate):** constrain to the artifacts directory rather than switching to artifact ids. Verified against the live DB: the three TTS profiles that set this field all store absolute paths under `artifacts/refs/` (e.g. `/Users/lugon/code/speech-text-transformer/artifacts/refs/omnivoice-nu-tre-ref.wav`), which are manually-placed files, not upload-generated ones. A containment check keeps them working; switching to ids would break them and require a migration.

Note `POST /v1/tts/reference-audio` (`tts.py:38-46`) returns `str(artifact_store.path_for(ref_id))` — an absolute path — so the client legitimately round-trips absolute paths. Your check must accept those.

Implement containment with `Path(...).resolve()` on BOTH sides and a `is_relative_to` comparison, so `..` traversal and symlinks are handled. Reject with a clear validation error naming the field.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_ref_audio_path_containment.py`:

```python
"""ref_audio_path must never escape the artifacts directory.

It reaches Path(...).read_bytes() in six providers, so an unvalidated value is
an arbitrary local file read (and, via http_tts, an exfiltration channel)."""

import pytest
from pydantic import ValidationError

from app.schemas.tts import TTSRequest
from app.services.artifacts import artifact_store


def _inside(name: str) -> str:
    return str((artifact_store.base_dir / name).resolve())


def test_accepts_path_inside_artifacts_dir():
    req = TTSRequest(text="hi", ref_audio_path=_inside("refs/voice.wav"))
    assert req.ref_audio_path is not None


def test_accepts_none():
    assert TTSRequest(text="hi").ref_audio_path is None


@pytest.mark.parametrize(
    "bad",
    [
        "/etc/passwd",
        "/dev/zero",
        "/app/.env",
        "../../../../etc/passwd",
        "relative/escape/../../../etc/passwd",
    ],
)
def test_rejects_paths_outside_artifacts_dir(bad):
    with pytest.raises(ValidationError):
        TTSRequest(text="hi", ref_audio_path=bad)


def test_rejects_traversal_that_starts_inside():
    escape = str(artifact_store.base_dir / ".." / ".." / "etc" / "passwd")
    with pytest.raises(ValidationError):
        TTSRequest(text="hi", ref_audio_path=escape)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_ref_audio_path_containment.py -v`
Expected: FAIL — every path is currently accepted.

- [ ] **Step 3: Write the implementation**

Add the containment helper to `artifacts.py` and a pydantic field validator on `TTSRequest.ref_audio_path` that calls it. Keep the error message specific ("ref_audio_path must be inside the artifacts directory").

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_ref_audio_path_containment.py -v`
Expected: PASS

- [ ] **Step 5: Verify the real stored profiles still validate**

The three live values all look like `<repo>/artifacts/refs/<name>.wav`. Construct that same shape in a test or a one-off check and confirm it passes validation. State the result in your report.

Run: `.venv/bin/pytest tests/unit/test_tts_profile_routes.py tests/unit/test_http_tts_provider.py tests/unit/test_tts_profile_model_gate.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/schemas/tts.py apps/api_gateway/app/services/artifacts.py tests/unit/test_ref_audio_path_containment.py
git commit -m "fix(tts): reject ref_audio_path outside the artifacts directory"
```

---

### Task 6: MCP — admin-only writes

**Files:**
- Modify: `apps/api_gateway/app/api/routes/mcp.py` (write routes only)
- Test: `tests/unit/test_mcp_ssrf.py`

**Interfaces:**
- Consumes: the repo's admin-check pattern (same one Task 3 uses).
- Produces: nothing downstream.

**Background:** `/v1/mcp` is in `_USER_PREFIXES`, so any logged-in non-admin can `POST /v1/mcp/servers` with an arbitrary `url` (only the scheme is checked) and custom headers, then `GET /v1/mcp/servers/{name}/tools` to make the gateway fetch it and **return the response body to them** — a full SSRF proxy with reflection, e.g. against `http://169.254.169.254`.

**Decisions already made (do not relitigate):**

- **Admin-only for create/update/delete/clone. NO IP blocklist.** A blocklist was considered and explicitly rejected: the only MCP server that exists in the live DB is `basic-tools` at `http://localhost:8090` (`owner_id` null — an admin-created template), and self-hosting an MCP server on loopback is the normal deployment pattern here. A private-address blocklist would break the one legitimate server while only raising the bar for an actor who must already be an admin.
- Migration cost is zero: there are **no user-owned MCP servers** in the DB today, so admin-gating writes breaks nobody.
- The durable fix for user-supplied tools is client-side MCP (the xiaozhi model), which this repo already implements for firmware in `apps/api_gateway/app/services/conversation/tools/device_mcp.py` — the gateway is an MCP *client* over the already-authenticated Lugo WebSocket, so there is no outbound connection and no SSRF surface at all. Extending that to the web client is **explicitly out of scope for this plan** and will be brainstormed separately; do not start it here.

Do not break the read routes for normal users (`GET /v1/mcp/servers`, `.../tools`) — only the write surface becomes admin-only.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_mcp_ssrf.py`:

```python
"""MCP servers are fetched by the gateway and their responses returned to the
caller -- so an arbitrary user-supplied URL is an SSRF proxy."""

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def _as_user(client, role: str):
    """Reuse this repo's existing session fixture -- find it first."""
    raise NotImplementedError("replace with this repo's existing session fixture")


def test_normal_user_cannot_create_mcp_server(client):
    _as_user(client, "user")
    resp = client.post("/v1/mcp/servers", json={
        "name": "ssrf", "url": "http://169.254.169.254", "headers": {},
    })
    assert resp.status_code == 403


def test_normal_user_can_still_list_servers(client):
    _as_user(client, "user")
    assert client.get("/v1/mcp/servers").status_code == 200


def test_normal_user_cannot_update_or_delete_or_clone(client):
    """The whole write surface moves to admin, not just create."""
    _as_user(client, "user")
    assert client.put("/v1/mcp/servers/basic-tools", json={
        "name": "basic-tools", "url": "http://attacker.example", "headers": {},
    }).status_code == 403
    assert client.delete("/v1/mcp/servers/basic-tools").status_code == 403
    assert client.post("/v1/mcp/servers/basic-tools/clone", json={"name": "copy"}).status_code == 403


def test_admin_can_still_create_a_loopback_server(client):
    """Deliberate: self-hosted MCP on loopback is the normal pattern here --
    the live `basic-tools` server is at http://localhost:8090. No IP blocklist."""
    _as_user(client, "admin")
    resp = client.post("/v1/mcp/servers", json={
        "name": "ok", "url": "http://localhost:8090", "headers": {},
    })
    assert resp.status_code in (200, 201)
```

Match the actual HTTP verbs and request bodies this repo's MCP routes use — read `mcp.py` first and adjust the calls above to fit; the assertions (403 for a normal user, success for an admin) are what matter.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_mcp_ssrf.py -v`
Expected: FAIL — normal users can create, and private URLs are accepted.

- [ ] **Step 3: Write the implementation**

Add the admin gate to the write routes and the address check to URL validation.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_mcp_ssrf.py -v`
Expected: PASS

- [ ] **Step 5: Run the MCP suites for regressions**

Run: `.venv/bin/pytest tests/unit -q -k mcp`
Expected: PASS. Existing tests that create MCP servers at loopback addresses must keep passing — there is no IP blocklist. If a test fails because it created a server as a non-admin, update that test's session role to admin; do not relax the gate.

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/api/routes/mcp.py tests/unit/test_mcp_ssrf.py
git commit -m "fix(mcp): restrict server create/update/delete/clone to admins"
```

---

### Task 7: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Run the whole suite**

Run: `.venv/bin/pytest tests/unit tests/integration -q`
Expected: 1540+ passed (the new tests add to the count), with only the one pre-existing `test_stt_ws.py` failure.

- [ ] **Step 2: Verify each Critical is actually closed, by hand**

Start the gateway from this worktree on a spare port against the real DB:

```bash
DATABASE_URL="sqlite+aiosqlite:////Users/lugon/code/speech-text-transformer/data/app.db" \
PYTHONPATH=apps/api_gateway:apps \
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8011
```

Then confirm, with NO credentials:

```bash
curl -s -o /dev/null -w "events:  %{http_code}\n" http://127.0.0.1:8011/v1/events/sessions/x
curl -s -o /dev/null -w "docs:    %{http_code}\n" http://127.0.0.1:8011/agents-docs
curl -s -o /dev/null -w "openapi: %{http_code}\n" http://127.0.0.1:8011/openapi.json
curl -s -o /dev/null -w "artifact:%{http_code}\n" http://127.0.0.1:8011/artifacts/x.wav
curl -s -o /dev/null -w "root:    %{http_code}\n" http://127.0.0.1:8011/
curl -s -o /dev/null -w "health:  %{http_code}\n" http://127.0.0.1:8011/health
```

Expected: the first four are 401/403; `/` and `/health` stay 200.

- [ ] **Step 3: Record the results in a report**

Write what you ran and what you saw to `.superpowers/sdd/2026-07-28-critical-authz-fixes/final-verification.md`. Report honestly — if something is still open, say so.

---

## Self-Review

**Coverage of the five Criticals + root cause:**
- Critical 1 (`POST /v1/conversation/llm` open to any user) → Task 3 ✓
- Critical 2 (`ref_audio_path` arbitrary file read) → Task 5 ✓
- Critical 3 (chat/WS `session_id` IDOR) → Task 4 ✓
- Critical 4 (MCP SSRF) → Task 6 ✓
- Critical 5 (`/v1/events` unauthenticated) → Task 1 (authentication) + Task 2 (ownership) ✓
- Root cause (default-allow middleware) → Task 1 ✓, which also closes `/agents-docs`, `/artifacts`, `/docs`, `/openapi.json` and the segment-boundary weakness.

**Explicitly OUT of scope** (audit findings deferred to a later pass, listed so nobody assumes they were handled): cookie sessions never revalidated against the DB; auth fail-open when `admin_password` is unset; profile IDOR in chat/WS/lugo/livehost; `POST /v1/stt/warm` global model switch; MCP template headers leaked to all users; provider `enabled=false` not honored by `resolve_credentials`; TTS stream quota checked only once; synchronous denoise blocking the event loop; upload size limits; STT `model` field bypassing the engine whitelist.

**Type consistency:** Task 1's `_matches` signature is unchanged (`(path, prefixes) -> bool`); only its semantics tighten. `_PUBLIC_PATHS` is a new module-level frozenset consumed only within `auth_guard.py`. Tasks 2-6 add no shared symbols across tasks.
