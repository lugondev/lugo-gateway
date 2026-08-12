# Home dashboard (stats/info) — design

## Goal

Add a "Home" tab to the admin console showing at-a-glance stats: how many
profiles/devices/sessions exist, model registry + currently-active
models, system health, and usage/quota — so an operator (or a regular user,
scoped to their own data) can see the state of the system without hunting
across tabs.

## Placement

- New nav item **"Home"**, first item in the sidebar (before Conversation),
  visible to every logged-in user (not `admin-only`).
- New default landing section on load: `initSidebar()`'s no-`?tab=` fallback
  changes from `conversation` to `home`. Deep links (`?tab=conversation`
  etc.) are unaffected.
- Content is role-scoped, not route-scoped: everyone sees the same tab, but
  admins see extra widgets regular users can't (mirrors how `/v1/system` and
  `/v1/model_registry` are already admin-only).

## Backend

### New: `GET /v1/stats/home`

New file `app/api/routes/stats.py`, router prefix `/v1/stats`. Registered in
`main.py` and added to `auth_guard._USER_PREFIXES` (reachable by any logged-in
session, same tier as `/v1/sessions` and `/v1/profiles`) — the handler itself
branches on role, it is not gated at the routing layer.

```
{
  "success": true,
  "data": {
    "profiles": { "count": <int> },
    "devices": { "count": <int>, "active_recent": <int> },
    "sessions": { "count": <int> }
  }
}
```

- `profiles.count`: same visibility rule `list_profiles` already applies
  (`profile_visible(profile, user_id)`), just counted instead of returned.
- `devices.count` / `active_recent`: admin gets `device_store.list_all()`;
  regular user gets their own devices (same store call `devices.py`'s
  `/mine` uses). `active_recent` counts rows whose `last_seen_at` is within
  30 minutes of now.
  - **Caveat, stated in the UI copy**: `last_seen_at` is only touched once
    per new WS handshake (`auth_guard.py`), not on a heartbeat, so this is a
    "recently active" proxy, not a live-online signal. Label it "Active
    recently" — never "Online" — so the number doesn't overclaim. A true
    live-online status needs a heartbeat, which is out of scope here.
- `sessions.count`: new `session_store.count(profile_id=None, user_id=None,
  source=None, client_id=None) -> int` method in
  `app/services/history/store.py`, mirroring `list()`'s filters but doing a
  plain `SELECT count(*)` instead of fetching rows. The route calls it with
  `user_id=scope_user_id(request)` — same scoping `list_sessions` already
  uses, so an admin gets the global total and a user gets their own.

No new schema needed (plain dict responses, matching every other route in
this file's neighborhood).

### Everything else: reuse, no backend changes

Admin-only widgets call existing admin endpoints directly from the Home
page's JS — these routes already 403 non-admins, so there's nothing to leak
by calling them unconditionally when `role === "admin"`:

- **Model registry summary** (total entries + breakdown by `kind`):
  `GET /v1/model_registry`, counted client-side from the existing list.
- **Active models**: `GET /v1/system/status` (`stt_engines`, `tts_engines`,
  `whisper_local.active_model`, `vosk.active_model_path`) +
  `GET /v1/models` (`data.llm.active`, `.available`, `.base_url`, `.remote`)
  — the exact fields `model-manager.js` and `system-status.js` already
  render elsewhere, reused as-is.
- **System health tiles**: same `sttOk`/`ttsOk` derivation
  `system-status.js` already does (`stt_engines.some(e => e.available)`),
  plus `llm.available` — shown as bigger tiles on Home, not recomputed
  differently.
- **Usage totals (admin)**: `GET /v1/usage/summary?group_by=kind` (existing,
  admin-only), summed client-side for "requests this month" / "cost this
  month".

Regular users' usage/quota widget reuses `GET /v1/usage/me` (existing,
already returns `limits` for quota progress).

## Frontend

New file `app/static/js/home.js`, exporting `loadHome()`, wired into
`sidebar-nav.js` the same way every other tab's loader is (`if (section ===
"home") loadHome();`) and called once eagerly at startup (since Home is now
the default landing section).

Layout, top to bottom:

1. **Overview row** (everyone): three stat tiles — Profiles, Devices
   (`count` · `active_recent` recently active), Sessions. Sourced from
   `/v1/stats/home`.
2. **Usage this month** (everyone): requests + cost tiles, plus quota
   progress bar(s) if any apply. Admin source: `/v1/usage/summary`. User
   source: `/v1/usage/me`.
3. **System health** (admin only): STT/TTS/LLM ready tiles.
4. **Active models** (admin only): STT active model + device, TTS engine,
   LLM active model + base_url/remote.
5. **Model registry** (admin only): total entries, breakdown by kind
   (stt/tts/llm counts), link to the Model Registry tab.

Sections 3–5 are simply not rendered (not just visually hidden) when
`role !== "admin"`, matching how `sidebar-nav.js` already knows the caller's
role from `fetchAuthStatus()`.

Errors: each widget fetches independently and fails independently (a
failed `/v1/usage/summary` call shows that one tile as "—" / error state,
it does not blank the rest of the page) — same pattern
`loadSystemStatus()` already uses (try/catch around the fetch, falls back to
an inline error tile).

## Testing

- `tests/unit/conversation/test_session_store.py` gets a case for the new
  `count()` method: matches `list()`'s row count for the same filters, and
  returns 0 on an empty table.
- New `tests/unit/http/test_stats_routes.py` mirroring
  `test_usage_routes.py`'s role-scoping style: admin sees the global count,
  a regular user sees only their own profiles/devices/sessions counted.
- `test_auth_guard_route_coverage.py` (the anti-omission harness) will fail
  until `/v1/stats` is classified in `auth_guard.py` — expected, and is the
  signal that the prefix was registered correctly.
- Manual: load Home as admin and as a regular user in the browser, confirm
  the admin-only sections are absent (not just hidden) for the regular
  user, and that landing on `/` with no `?tab=` opens Home.
