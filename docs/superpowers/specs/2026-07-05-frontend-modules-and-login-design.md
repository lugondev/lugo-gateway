# Design: split `app.js` into ES modules + browser login skeleton

## Context

`apps/api_gateway/app/static/app.js` is a single 2587-line vanilla JS file loaded via a plain
`<script src="/static/app.js">` — no bundler, no build step, static files served directly by
FastAPI's `StaticFiles`. The file is already organized into clearly delimited sections (by
`// ====` comments): helpers, audio capture, system status, model manager, model recommender, STT
batch/stream, TTS engines/batch/stream, conversation, chat, profiles, MCP servers, sessions,
sidebar nav. State is mostly already scoped per-section (each section owns its own top-level
`let`/`const` state), which makes the split tractable.

Separately, the control panel is planned to get a login gate before broader use (currently
anyone who can reach the deployed URL can use the full admin UI). There is
no user/auth model in the backend today.

## Scope

1. Split `static/app.js` into ES modules under `static/js/`, one module per existing section,
   loaded via `<script type="module" src="/static/js/main.js">`.
2. Add a login skeleton: single shared password (`ADMIN_PASSWORD` env var), signed cookie
   session (Starlette `SessionMiddleware`), a login page, and a guard that protects the browser
   control panel + admin API routes.

## Not in scope

- No per-user accounts / user table — single shared password only.
- No auth for device-facing endpoints (`/v1/conversation/stream`, `/v1/stt/*`, `/v1/tts/*`,
  `/v1/events/*`) used directly by ESP32/RPi clients — those stay open in this change.
- **Future work**: device auth (ESP32/RPi) will use per-client API keys, issued and checked
  separately from the browser session cookie — a later, separate task.
- No framework migration (Vue/Alpine) — module split stays vanilla JS/ES modules.
- No changes to the 42 existing `fetch()` call sites, WebSocket, or EventSource call sites — all
  are same-origin, so the browser attaches the session cookie automatically without code changes.

## Frontend module split

New `static/js/` directory, `index.html` script tag changes to
`<script type="module" src="/static/js/main.js"></script>`.

| Module | Contents (from existing `app.js` sections) |
|---|---|
| `helpers.js` | `pretty, print, el, fmtBytes, wsUrl, loadPrefs, savePref, controlValue, restoreAndBind` |
| `audio-capture.js` | `downsampleToPcm16, writeStr, encodeWav, createMicCapture` |
| `system-status.js` | System Status panel rendering |
| `base-context.js` | `getPreproc` + base context state |
| `model-manager.js` | model row rendering/binding, download jobs |
| `model-recommender.js` | recommend panel |
| `stt-engines.js` | `updateEngineDetail` (STT dropdown) |
| `stt-batch.js` | upload/record STT |
| `stt-stream.js` | STT WebSocket streaming |
| `tts-engines.js` | TTS dropdown + voices |
| `tts-batch.js` | TTS batch |
| `tts-stream.js` | TTS SSE + progressive playback |
| `conversation.js` | mic-to-mic conversation WebSocket |
| `chat.js` | text chat + chat modes + voice-to-text/text-to-voice |
| `profiles.js` | profile panel + profile memory |
| `mcp-servers.js` | MCP server list/config |
| `sessions.js` | sessions panel |
| `sidebar-nav.js` | sidebar init/toggle |
| `auth.js` (new) | login state, global `fetch` 401-redirect wrapper, logout |
| `main.js` | entry point: imports the above, wires `DOMContentLoaded`, status badges |

Cross-module state (`currentSessionId`, `profileData`, `mcpServerData`) stays owned by the module
whose feature drives it (`chat.js`, `profiles.js`, `mcp-servers.js` respectively) and is exported
for other modules to import — no separate generic `state.js`.

## Backend auth

**Settings** (`app/core/settings.py`):
- `admin_password: str = ""` — from env `ADMIN_PASSWORD`. Empty means auth is fully disabled
  (guard middleware is a no-op), so local dev and the existing test suite are unaffected.
- `session_secret: str = ""` — from env `SESSION_SECRET`. If empty while `admin_password` is
  set, a random secret is generated at process startup (sessions are invalidated on restart —
  acceptable for this skeleton).

**New route** `app/api/routes/auth.py`:
- `POST /api/auth/login` — body `{password}`, constant-time compare against
  `settings.admin_password`; on success sets `request.session["authenticated"] = True`.
- `POST /api/auth/logout` — clears the session.
- `GET /api/auth/status` — returns `{authenticated: bool}` for the frontend to check on load.

**Guard**: one `BaseHTTPMiddleware` added in `main.py`, after `SessionMiddleware`:
- No-op entirely when `settings.admin_password == ""`.
- Allow-list (no login required): `/api/auth/*`, `/health`, `/static/login.html`,
  `/static/js/auth.js`, `/static/styles.css`, and all device-facing routes
  (`/v1/conversation/stream`, `/v1/stt/*`, `/v1/tts/*`, `/v1/events/*`).
- Everything else under the control panel surface (`/ui`, `/static/index.html`,
  `/static/js/*` other than `auth.js`, `/v1/system`, `/v1/recommend`, `/v1/profiles`,
  `/v1/mcp`, `/v1/sessions`, `/v1/memories`) requires
  `request.session.get("authenticated")`; missing session returns `401` for API-style requests
  or redirects to `/static/login.html` for `text/html` requests.

## Error handling

- Wrong password → `401 {"success": false, "error": "invalid password"}` (no user enumeration;
  there is only one account).
- `ADMIN_PASSWORD` unset in a non-empty deploy → log a startup warning
  ("auth disabled: ADMIN_PASSWORD not set") so an open panel is never silent.
- Tampered/expired cookie → `SessionMiddleware` treats it as unauthenticated (bad signature).
- Frontend: `auth.js` installs a global `fetch` wrapper that redirects to
  `/static/login.html` on any `401` response, so the other 42 call sites need no changes.
  WebSocket/EventSource failures caused by an expired session surface through each module's
  existing error handling — no new logic needed there.

## Testing

- Unit tests for `auth.py`: correct/incorrect password, `/api/auth/status` before/after login,
  logout clears session, guard middleware no-ops when `admin_password` is empty, guard blocks an
  admin route when logged out, allow-listed device routes remain reachable without login.
- No JS test harness exists in this project; the module split and login flow are verified
  manually in the browser: `/ui` unauthenticated redirects to login; correct password reaches the
  UI; all existing tabs/features still work after the split.
