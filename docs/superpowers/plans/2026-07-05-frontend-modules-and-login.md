# Frontend Module Split + Login Skeleton Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split `apps/api_gateway/app/static/app.js` (2587 lines) into per-feature ES modules, and add a single-shared-password login skeleton that gates the browser control panel (not the device-facing STT/TTS/conversation streaming endpoints).

**Architecture:** Backend gets a `SessionMiddleware` (signed cookie) + `/api/auth/*` routes + one path-based guard middleware. Frontend gets a new `static/js/` directory of ES modules (one per existing `// ====` section in `app.js`) plus `static/login.html` + `static/js/auth.js`. Same-origin cookies mean the existing 42 `fetch()` call sites need no changes.

**Tech Stack:** FastAPI/Starlette (`SessionMiddleware`, `BaseHTTPMiddleware`), vanilla ES modules (no bundler), pytest + `TestClient`.

## Global Constraints

- `admin_password` empty (default) ⇒ auth is fully disabled (no-op guard) — local dev and the existing test suite must be unaffected.
- Device-facing routes stay unauthenticated in this change: `/v1/conversation/stream`, `/v1/stt/*`, `/v1/tts/*`, `/v1/events/*`, `/agents-docs`.
- No new pip/npm dependencies — `SessionMiddleware` and its `itsdangerous` dependency ship with `starlette`, already a transitive dependency of `fastapi`.
- No bundler/build step — plain `<script type="module">`.
- Full spec: `docs/superpowers/specs/2026-07-05-frontend-modules-and-login-design.md`.

---

## Task 1: Auth settings + session middleware + login/logout/status routes

**Files:**
- Modify: `apps/api_gateway/app/core/settings.py`
- Modify: `apps/api_gateway/app/core/errors.py`
- Modify: `apps/api_gateway/app/main.py`
- Create: `apps/api_gateway/app/api/routes/auth.py`
- Test: `tests/unit/test_auth_routes.py`

**Interfaces:**
- Produces: `settings.admin_password: str`, `settings.session_secret: str`; `AuthError(AppError)` with `status_code = 401`; `app.api.routes.auth.router` mounted at `/api/auth` with `POST /login`, `POST /logout`, `GET /status`; `request.session["authenticated"]: bool` (Starlette session dict, available on every request once `SessionMiddleware` is installed).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_auth_routes.py`:

```python
import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def test_status_unauthenticated_by_default(client, _with_password):
    resp = client.get("/api/auth/status")
    assert resp.status_code == 200
    assert resp.json() == {"authenticated": False}


def test_login_wrong_password_rejected(client, _with_password):
    resp = client.post("/api/auth/login", json={"password": "nope"})
    assert resp.status_code == 401
    assert resp.json()["success"] is False


def test_login_correct_password_sets_session(client, _with_password):
    resp = client.post("/api/auth/login", json={"password": "s3cret"})
    assert resp.status_code == 200
    assert client.get("/api/auth/status").json() == {"authenticated": True}


def test_logout_clears_session(client, _with_password):
    client.post("/api/auth/login", json={"password": "s3cret"})
    client.post("/api/auth/logout")
    assert client.get("/api/auth/status").json() == {"authenticated": False}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/api_gateway && python -m pytest ../../tests/unit/test_auth_routes.py -v`
Expected: FAIL / ERROR — `app.api.routes.auth` does not exist yet, `settings.admin_password` does not exist yet.

- [ ] **Step 3: Add settings fields**

In `apps/api_gateway/app/core/settings.py`, add after the `cors_allow_origins: str = "*"` line:

```python
    # Browser control-panel login (single shared password). Empty = auth disabled.
    admin_password: str = ""
    # Cookie-signing secret for the login session. Empty (with admin_password set)
    # -> a random secret is generated at process startup (sessions reset on restart).
    session_secret: str = ""
```

- [ ] **Step 4: Add `AuthError`**

In `apps/api_gateway/app/core/errors.py`, append:

```python
class AuthError(AppError):
    """Raised when login credentials are invalid or a session is required."""

    status_code = 401
```

- [ ] **Step 5: Create the auth routes**

Create `apps/api_gateway/app/api/routes/auth.py`:

```python
import hmac

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.core.errors import AuthError
from app.core.settings import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    password: str


@router.post("/login")
async def login(body: LoginRequest, request: Request) -> dict:
    if not settings.admin_password or not hmac.compare_digest(body.password, settings.admin_password):
        raise AuthError("invalid password")
    request.session["authenticated"] = True
    return {"success": True}


@router.post("/logout")
async def logout(request: Request) -> dict:
    request.session.clear()
    return {"success": True}


@router.get("/status")
async def status(request: Request) -> dict:
    return {"authenticated": bool(request.session.get("authenticated"))}
```

- [ ] **Step 6: Wire `SessionMiddleware` and the auth router into `main.py`**

In `apps/api_gateway/app/main.py`:

Add imports (near the top, with the other stdlib/fastapi imports):

```python
import secrets
```
```python
from starlette.middleware.sessions import SessionMiddleware
```
```python
from app.api.routes.auth import router as auth_router
```

After the existing `app.add_middleware(CORSMiddleware, ...)` block, add:

```python
_session_secret = settings.session_secret or secrets.token_hex(32)
app.add_middleware(SessionMiddleware, secret_key=_session_secret, same_site="lax")
```

Add the router with the other `app.include_router(...)` calls:

```python
app.include_router(auth_router)
```

Finally, warn at boot if the control panel is effectively public outside dev (mirrors the existing `logger.warning(...)` style already used in `_warm_default_engines`). Add near the top of the `lifespan` function, right after `await init_db()`:

```python
    if not settings.admin_password and settings.app_env != "dev":
        logger.warning("auth disabled: ADMIN_PASSWORD not set (app_env=%s)", settings.app_env)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd apps/api_gateway && python -m pytest ../../tests/unit/test_auth_routes.py -v`
Expected: PASS (4 tests)

- [ ] **Step 8: Run the full unit suite to check for regressions**

Run: `cd apps/api_gateway && python -m pytest ../../tests/unit -q`
Expected: PASS, same count as before plus 4

- [ ] **Step 9: Commit**

```bash
git add apps/api_gateway/app/core/settings.py apps/api_gateway/app/core/errors.py apps/api_gateway/app/main.py apps/api_gateway/app/api/routes/auth.py tests/unit/test_auth_routes.py
git commit -m "feat: add admin login session (settings, auth routes, SessionMiddleware)"
```

---

## Task 2: Guard middleware protecting the admin control panel

**Files:**
- Create: `apps/api_gateway/app/core/auth_guard.py`
- Modify: `apps/api_gateway/app/main.py`
- Test: `tests/unit/test_auth_guard.py`

**Interfaces:**
- Consumes: `settings.admin_password` (Task 1), `request.session` (Task 1, via `SessionMiddleware`).
- Produces: `AuthGuardMiddleware` (Starlette `BaseHTTPMiddleware` subclass) exported from `app.core.auth_guard`.

**Guarded paths:** `/ui`, `/static/*` (except `/static/login.html`, `/static/js/auth.js`, `/static/styles.css`), `/v1/system`, `/v1/models`, `/v1/profiles`, `/v1/mcp`, `/v1/sessions` (this last set covers every router in `apps/api_gateway/app/api/routes/{system,recommend,profiles,mcp,sessions,memories}.py` — `recommend.py`'s routes live under `/v1/models/*`, and `memories.py`'s routes live under `/v1/profiles/{name}/memories`, both already covered by the `/v1/models` and `/v1/profiles` prefixes). Everything else (`/health`, `/agents-docs`, `/v1/conversation/*`, `/v1/stt/*`, `/v1/tts/*`, `/v1/events/*`, `/api/auth/*`) stays open.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_auth_guard.py`:

```python
import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_guard_noop_when_admin_password_unset(client):
    assert settings.admin_password == ""
    resp = client.get("/v1/system/status")
    assert resp.status_code != 401


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def test_guard_blocks_system_route_when_logged_out(client, _with_password):
    resp = client.get("/v1/system/status")
    assert resp.status_code == 401


def test_guard_blocks_models_route_when_logged_out(client, _with_password):
    resp = client.get("/v1/models")
    assert resp.status_code == 401


def test_guard_allows_system_route_after_login(client, _with_password):
    client.post("/api/auth/login", json={"password": "s3cret"})
    resp = client.get("/v1/system/status")
    assert resp.status_code != 401


def test_guard_allows_device_routes_without_login(client, _with_password):
    resp = client.get("/v1/stt/engines")
    assert resp.status_code != 401


def test_guard_allows_auth_routes_without_login(client, _with_password):
    resp = client.get("/api/auth/status")
    assert resp.status_code != 401
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/api_gateway && python -m pytest ../../tests/unit/test_auth_guard.py -v`
Expected: FAIL — `/v1/system/status` and `/v1/models` return 200 even without login (guard doesn't exist yet).

- [ ] **Step 3: Create the guard middleware**

Create `apps/api_gateway/app/core/auth_guard.py`:

```python
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from app.core.settings import settings

_STATIC_ALLOWLIST = {"/static/login.html", "/static/js/auth.js", "/static/styles.css"}
_GUARDED_PREFIXES = (
    "/ui",
    "/static/",
    "/v1/system",
    "/v1/models",
    "/v1/profiles",
    "/v1/mcp",
    "/v1/sessions",
)


class AuthGuardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not settings.admin_password:
            return await call_next(request)

        path = request.url.path
        if path in _STATIC_ALLOWLIST or path.startswith("/api/auth"):
            return await call_next(request)

        if any(path == prefix or path.startswith(prefix) for prefix in _GUARDED_PREFIXES):
            if not request.session.get("authenticated"):
                if "text/html" in request.headers.get("accept", ""):
                    return RedirectResponse("/static/login.html")
                return JSONResponse({"success": False, "error": "login required"}, status_code=401)

        return await call_next(request)
```

- [ ] **Step 4: Wire the middleware into `main.py`, ahead of `SessionMiddleware`**

Middleware added later becomes outermost (runs first). `AuthGuardMiddleware` reads `request.session`, so `SessionMiddleware` must run *before* it on every request, which means `SessionMiddleware` must be added *after* `AuthGuardMiddleware` — i.e. `AuthGuardMiddleware`'s `add_middleware` call must sit **above** the `SessionMiddleware` one already in the file.

Add the import:

```python
from app.core.auth_guard import AuthGuardMiddleware
```

Replace:

```python
_session_secret = settings.session_secret or secrets.token_hex(32)
app.add_middleware(SessionMiddleware, secret_key=_session_secret, same_site="lax")
```

with:

```python
app.add_middleware(AuthGuardMiddleware)
_session_secret = settings.session_secret or secrets.token_hex(32)
app.add_middleware(SessionMiddleware, secret_key=_session_secret, same_site="lax")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd apps/api_gateway && python -m pytest ../../tests/unit/test_auth_guard.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Run the full unit suite to check for regressions**

Run: `cd apps/api_gateway && python -m pytest ../../tests/unit -q`
Expected: PASS, no regressions (existing tests hit `/v1/*` routes with `admin_password` defaulting to `""`, so the guard no-ops for them)

- [ ] **Step 7: Commit**

```bash
git add apps/api_gateway/app/core/auth_guard.py apps/api_gateway/app/main.py tests/unit/test_auth_guard.py
git commit -m "feat: guard admin control-panel routes behind login session"
```

---

## Task 3: Login page + `auth.js` (standalone, independent of the module split)

**Files:**
- Create: `apps/api_gateway/app/static/login.html`
- Create: `apps/api_gateway/app/static/js/auth.js`
- Modify: `apps/api_gateway/app/static/index.html`
- Modify: `apps/api_gateway/app/static/styles.css`

**Interfaces:**
- Consumes: `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/status` (Task 1).
- Produces: `static/js/auth.js` exports nothing (self-initializing based on which DOM elements are present) — later consumed by `main.js` in Task 4 via `import "./auth.js";`.

This task does not touch `app.js` — `index.html` keeps its existing `<script src="/static/app.js">` tag and gets a *second*, independent `<script type="module">` tag for `auth.js`, so the login flow is fully testable before the module split happens.

- [ ] **Step 1: Create the login page**

Create `apps/api_gateway/app/static/login.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Speech Text Transformer — Login</title>
  <link rel="stylesheet" href="/static/styles.css" />
</head>
<body>
  <div class="login-page">
    <form id="login-form" class="login-card">
      <h1>Speech&nbsp;Text&nbsp;Transformer</h1>
      <input type="password" id="login-password" placeholder="Password" autocomplete="current-password" autofocus />
      <button type="submit">Log in</button>
      <div id="login-status" class="login-status"></div>
    </form>
  </div>
  <script type="module" src="/static/js/auth.js"></script>
</body>
</html>
```

- [ ] **Step 2: Add minimal login page styling**

Append to `apps/api_gateway/app/static/styles.css`:

```css
.login-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
}
.login-card {
  display: flex;
  flex-direction: column;
  gap: 12px;
  width: 280px;
  padding: 24px;
  border: 1px solid var(--border, #333);
  border-radius: 8px;
}
.login-card h1 {
  font-size: 16px;
  margin: 0 0 8px;
  text-align: center;
}
.login-status {
  min-height: 18px;
  color: #e05555;
  font-size: 13px;
}
```

(If the project has no `--border` CSS variable, use a literal color — check the top of `styles.css` for existing custom properties and match whatever token is already used for panel borders.)

- [ ] **Step 3: Create `auth.js`**

Create `apps/api_gateway/app/static/js/auth.js`:

```js
const ORIGINAL_FETCH = window.fetch.bind(window);

function installUnauthorizedRedirect() {
  window.fetch = async (...args) => {
    const resp = await ORIGINAL_FETCH(...args);
    if (resp.status === 401 && !window.location.pathname.endsWith("/login.html")) {
      window.location.href = "/static/login.html";
    }
    return resp;
  };
}

async function handleLoginSubmit(e) {
  e.preventDefault();
  const password = document.getElementById("login-password").value;
  const status = document.getElementById("login-status");
  status.textContent = "";
  const resp = await ORIGINAL_FETCH("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
  if (resp.ok) {
    window.location.href = "/ui";
  } else {
    status.textContent = "Invalid password";
  }
}

async function handleLogout() {
  await ORIGINAL_FETCH("/api/auth/logout", { method: "POST" });
  window.location.href = "/static/login.html";
}

const loginForm = document.getElementById("login-form");
if (loginForm) {
  loginForm.addEventListener("submit", handleLoginSubmit);
} else {
  installUnauthorizedRedirect();
  const logoutBtn = document.getElementById("btn-logout");
  if (logoutBtn) logoutBtn.addEventListener("click", handleLogout);
}
```

- [ ] **Step 4: Add a logout button and wire `auth.js` into `index.html`**

In `apps/api_gateway/app/static/index.html`, inside `<div class="hdr-badges">`, add a logout button alongside the existing badges:

```html
          <button class="hdr-badge" id="btn-logout" title="Log out">&#9211; Logout</button>
```

Add a second script tag right after the existing `<script src="/static/app.js"></script>` line (do not remove that line yet — Task 4 replaces it):

```html
    <script type="module" src="/static/js/auth.js"></script>
```

- [ ] **Step 5: Manually verify the login flow**

Run: `cd apps/api_gateway && ADMIN_PASSWORD=s3cret uvicorn app.main:app --reload --port 8000` (adjust to however this repo normally starts the dev server — check `README.md` / `Makefile` for the actual run command if different)

Then:
1. Open `http://localhost:8000/ui` — expect a redirect to `/static/login.html` (guard from Task 2).
2. Enter the wrong password — expect "Invalid password" and no navigation.
3. Enter `s3cret` — expect redirect to `/ui`, page loads normally.
4. Click "Logout" — expect redirect back to `/static/login.html`.
5. Restart the server *without* `ADMIN_PASSWORD` set — `/ui` should load directly with no redirect (guard disabled).

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/static/login.html apps/api_gateway/app/static/js/auth.js apps/api_gateway/app/static/index.html apps/api_gateway/app/static/styles.css
git commit -m "feat: add login page and session-aware fetch wrapper"
```

---

## Task 4: Split `app.js` into ES modules

**Files:**
- Create: 18 files under `apps/api_gateway/app/static/js/` (listed below)
- Create: `apps/api_gateway/app/static/js/main.js`
- Modify: `apps/api_gateway/app/static/index.html`
- Delete: `apps/api_gateway/app/static/app.js`

**Interfaces:**
- Consumes: `auth.js` (Task 3, imported by `main.js`).
- Produces: nothing consumed by later tasks — this is the last task.

`app.js` is a classic (non-module) script with global-scope functions; nothing outside it references those names via inline `onclick=` attributes (verified: `grep -c 'onclick=\|onchange=\|oninput=' index.html` → `0`), so the whole file can move to ES modules in one atomic cutover. There is no way to do this file-by-file while keeping the site working, because a classic script and ES modules don't share scope — so this task moves every section at once, then verifies.

**Global rule for every extraction step below:** export *every* top-level `function`, `const`, and `let` declared in the moved range (prefix with `export `). An unused `export` is harmless; a missing one causes a `ReferenceError` you'd have to come back and fix anyway.

### Step 1: Create the module boundary table

Create `apps/api_gateway/app/static/js/` (`mkdir -p apps/api_gateway/app/static/js`).

For each row, find the *current* line numbers with `grep -n '<start marker>\|<end marker>' apps/api_gateway/app/static/app.js` (line numbers shift as you go — always re-grep, don't reuse numbers from an earlier extraction), then copy the lines from just after the start marker up to (not including) the end marker into the target file.

| Target file | Start marker (exclusive) | End marker (exclusive) |
|---|---|---|
| `helpers.js` | *(start of file)* | `// ============================================================ audio capture` |
| `audio-capture.js` | `// ============================================================ audio capture` | `// ============================================================ system status` |
| `system-status.js` | `// ============================================================ system status` | `// ============================================================ base context` |
| `base-context.js` | `// ============================================================ base context` | `// ============================================================ model manager` |
| `model-manager.js` | `// ============================================================ model manager` | `// ============================================================ model recommender` |
| `model-recommender.js` | `// ============================================================ model recommender` | `// ============================================================ STT engines list` |
| `stt-engines.js` | `// ============================================================ STT engines list` | `// ============================================================ STT batch (file or recording)` |
| `stt-batch.js` | `// ============================================================ STT batch (file or recording)` | `// ============================================================ STT streaming (WebSocket)` |
| `stt-stream.js` | `// ============================================================ STT streaming (WebSocket)` | `// ============================================================ TTS engines + voices` |
| `tts-engines.js` | `// ============================================================ TTS engines + voices` | `// ============================================================ TTS batch` |
| `tts-batch.js` | `// ============================================================ TTS batch` | `// ============================================================ TTS stream (SSE) + progressive playback` |
| `tts-stream.js` | `// ============================================================ TTS stream (SSE) + progressive playback` | `// ============================================================ conversation` |
| `conversation.js` | `// ============================================================ conversation` | `// ============================================================ LLM text chat` |
| `chat.js` (part 1) | `// ============================================================ LLM text chat` | `// ============================================================ sidebar nav` |
| `sidebar-nav.js` | `// ============================================================ sidebar nav` | `// ============================================================ module-level state (must precede init)` |
| *(exception — see Step 2)* | `// ============================================================ module-level state (must precede init)` | `// ============================================================ init` |
| *(main.js — see Step 3)* | `// ============================================================ init` | `// ============================================================ status badges` |
| *(exception — see Step 2)* | `// ============================================================ status badges` | `// ============================================================ chat modes` |
| `chat.js` (part 2, append) | `// ============================================================ chat modes` | `// ============================================================ profiles` |
| `profiles.js` (part 1) | `// ============================================================ profiles` | `// ============================================================ profile memory` |
| `profiles.js` (part 2, append) | `// ============================================================ profile memory` | `// ============================================================ sessions panel` |
| `sessions.js` | `// ============================================================ sessions panel` | `// ============================================================ MCP servers` |
| `mcp-servers.js` | `// ============================================================ MCP servers` | `// ============================================================ voice→text (in chat section)` |
| `chat.js` (part 3, append) | `// ============================================================ voice→text (in chat section)` | *(end of file)* |

`chat.js` and `profiles.js` are each assembled from multiple non-contiguous ranges (append part 2 and part 3 below part 1, in the order listed) — everything else is one contiguous cut.

### Step 2: Handle the two exceptions

The `module-level state (must precede init)` block (6 lines: `CHAT_MODES`, `chatMode`, `profileData`, `profileEditMode`, `mcpServerData`, `v2t`) and the `status badges` block (`setBadge`) don't belong in one file each — redistribute them:

- Into `chat.js` (top, before the rest of its content): `export const CHAT_MODES = {...}`, `export let chatMode = "text-text";`, `export const v2t = { ws: null, capture: null };`
- Into `profiles.js` (top): `export let profileData = {};`, `export let profileEditMode = null;`
- Into `mcp-servers.js` (top): `export let mcpServerData = {};`
- Into `helpers.js` (anywhere): the `setBadge` function, exported.

### Step 3: Author `main.js`

Create `apps/api_gateway/app/static/js/main.js`. It replaces the `// ==== init` block (lines between the `init` and `status badges` markers) with the same call sequence, plus imports for every name that sequence calls, plus `auth.js`:

```js
import "./auth.js";
import { restoreAndBind } from "./helpers.js";
import { initSidebar } from "./sidebar-nav.js";
import { loadSttEngines } from "./stt-engines.js";
import { initSttMode } from "./stt-batch.js";
import { initChatModes } from "./chat.js";
import { setStreamUI } from "./stt-stream.js";
import { setConvUI, loadConversationEngines } from "./conversation.js";
import { loadTtsEngines } from "./tts-engines.js";
import { loadSystemStatus } from "./system-status.js";
import { loadModels } from "./model-manager.js";
import { loadRecommend, loadLlmOnlineConfig } from "./model-recommender.js";
import { loadProfiles } from "./profiles.js";
import { loadMcpServers } from "./mcp-servers.js";

initSidebar();
initSttMode();
initChatModes();
setStreamUI("idle");
setConvUI("idle");
["stt-language", "stt-stream-language"].forEach(restoreAndBind);
loadSttEngines();
loadTtsEngines();
loadConversationEngines();
loadSystemStatus();
loadModels();
loadRecommend();
loadLlmOnlineConfig();
loadProfiles();
loadMcpServers();
```

(`initSttMode`/`loadSttEngines` may end up in different files depending on how the extraction actually lands — the *names* are correct per the boundary table above; double check against your extracted files and adjust the `from "./..."` paths if a name landed somewhere slightly different, e.g. `loadSttEngines` is defined in the `stt-engines.js` range per the table.)

### Step 4: Add the known cross-module imports

These are calls from one feature's raw section into another's, found while reading the file — add them now instead of waiting for a console error:

- `sidebar-nav.js` calls `loadRecommend()` and `loadMcpServers()` from its click handler — add:
  ```js
  import { loadRecommend } from "./model-recommender.js";
  import { loadMcpServers } from "./mcp-servers.js";
  ```
- Every module that calls `el`, `print`, `pretty`, `fmtBytes`, `wsUrl`, `loadPrefs`, `savePref`, `controlValue`, `restoreAndBind`, or `setBadge` needs `import { ... } from "./helpers.js";` — check each new file for these names and import accordingly.
- Every module that calls `downsampleToPcm16`, `encodeWav`, or `createMicCapture` needs `import { ... } from "./audio-capture.js";` (expected in `stt-batch.js`, `stt-stream.js`, `conversation.js`, and the voice-to-text part of `chat.js`).
- Every module that calls `getPreproc` needs `import { getPreproc } from "./base-context.js";` (expected in `stt-batch.js`, `stt-stream.js`, `conversation.js`).
- `profiles.js` likely reads `mcpServerData` while building the per-profile MCP server checklist — add `import { mcpServerData } from "./mcp-servers.js";` if `grep -n mcpServerData apps/api_gateway/app/static/js/profiles.js` shows a read beyond its own declaration.

### Step 5: Cut over `index.html`

Replace both:
```html
    <script src="/static/app.js"></script>
```
and (added in Task 3):
```html
    <script type="module" src="/static/js/auth.js"></script>
```
with a single line:
```html
    <script type="module" src="/static/js/main.js"></script>
```

### Step 6: Delete the old file

```bash
git rm apps/api_gateway/app/static/app.js
```

### Step 7: Console-driven fix-up loop

Start the dev server (same command as Task 3 Step 5) and open `http://localhost:8000/ui` with the browser devtools console open.

- If you see `Uncaught SyntaxError` in a module file, fix the syntax at that location (usually a missed line from a section boundary).
- If you see `Uncaught ReferenceError: X is not defined`, find the owning module with:
  ```bash
  grep -rn "^export function \?$(printf '%s' X)\|^export const \?$(printf '%s' X)\|^export let \?$(printf '%s' X)" apps/api_gateway/app/static/js/
  ```
  (or just `grep -rln "export.*\bX\b" apps/api_gateway/app/static/js/`), then add `import { X } from "./<that-file>.js";` to the file that referenced it.
- Reload after each fix. Repeat until the console is clean on initial page load.

### Step 8: Manual smoke test

Exercise every sidebar tab and confirm it behaves exactly as before the split (no functional changes were made, only file boundaries):

- [ ] System status panel loads (Env/TTS/STT tiles)
- [ ] System base context loads and "Saved ✓" appears after editing + saving
- [ ] Models tab: recommend list renders, a model row's install/select buttons work
- [ ] Chat tab, Text↔Text mode: send a message, get a reply
- [ ] Chat tab, Voice↔Voice mode: mic capture starts/stops, no console errors
- [ ] Chat tab, Voice→Text mode: live transcription updates
- [ ] Chat tab, Text→Voice mode: synthesize + play back
- [ ] STT tab: batch upload/record transcribes; streaming connects and shows partials
- [ ] TTS tab: batch synth plays; streaming SSE plays progressively
- [ ] Conversation tab: mic↔mic round trip works, bubbles render
- [ ] Profiles panel: create/edit a profile, memory list renders
- [ ] MCP servers panel: list renders, enable/disable toggle works
- [ ] Sessions panel: list renders, loading a past session populates chat history
- [ ] Sidebar collapse/expand toggle still works
- [ ] Header badges (STT/TTS/LLM) still update
- [ ] With `ADMIN_PASSWORD` set: `/ui` redirects to login when logged out, works normally when logged in, "Logout" button returns to the login page

### Step 9: Run the backend test suite once more

Run: `cd apps/api_gateway && python -m pytest ../../tests/unit -q`
Expected: PASS, unchanged (this task touches no Python)

### Step 10: Commit

```bash
git add apps/api_gateway/app/static/js apps/api_gateway/app/static/index.html
git commit -m "refactor: split app.js into per-feature ES modules"
```
