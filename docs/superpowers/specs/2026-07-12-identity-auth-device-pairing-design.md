# Identity, auth & device pairing foundation

Product branding for the UI pieces of this spec: **Lugo** (wordmark), product name
**Lugo BOT**. This round is a text-level rename onto the existing visual system (see
Component 7) — a full brand pass (logo asset, motion, tuned palette) is explicitly
deferred; the priority here is layout/IA, not polish.

## Problem

The gateway has exactly one tier of access today: a single shared `admin_password`
(`app/core/settings.py`) gates the whole UI, and a single shared `device_auth_token`
gates ESP32/RPi WebSocket clients (`app/core/auth_guard.py:ws_authenticated`). There is
no `users` table, no per-identity session, no roles, and no device registry anywhere in
the schema — every config store (`Profile`, `TtsProfile`, `McpServer`, `SystemConfig`)
and every conversation/livehost session is process-global, not owned by anyone.

The target product needs real multi-tenant users: an admin tier (system/model/engine
management, user management) and a user tier (each person logs in, has their own
chat/livehost/profile/device setup). This spec covers only the foundation everything
else depends on — identity, auth, roles, and basic device pairing — not the ownership
scoping of existing resources (profiles, conversations, livehost, MCP servers), which
are separate follow-up specs (see **Follow-ups**, below).

## Scope

**In scope:**
1. `users` table — real accounts (username/password), roles (`admin`/`user`), a
   `can_use_testing` flag (consumed by a later model-registry spec), `disabled` flag.
2. Self-signup, login/logout, session-cookie auth carrying `user_id`/`role` (replacing
   the single boolean `authenticated` flag). Admin bootstrap on first run.
3. Route-level authorization split: admin-only prefixes vs. any-authenticated-user
   prefixes, replacing the current single allowlist.
4. A periodic disabled/revoked re-check on long-lived WS sessions (browser and device),
   reusing the existing idle-timeout watchdog pattern, so `disabled=true` /
   `devices.revoked=true` cuts an already-open connection within tens of seconds — not
   just at the next login/connect attempt.
5. `devices` table + a minimal pairing flow (device shows a short code, a logged-in user
   claims it on the web) so an ESP32/RPi WS connection resolves to a specific `user_id`,
   not just "some device."
6. Fix an existing gap found during investigation: `/v1/lugo/stream` never calls
   `ws_authenticated()` (unlike `/v1/conversation/stream`, `/v1/stt/stream`,
   `/v1/livehost/stream`) — add the same check for consistency, now that auth is being
   reworked anyway.
7. UI for everything above, rebranded as Lugo/Lugo BOT: login+signup on one screen,
   role-gated sidebar additions ("Users", "Devices"), a Users management page, and a
   Devices page (own devices + claim UI for everyone, plus an all-devices table for
   admins). Ships inside the existing single-page app — not a separate admin/user app
   (that split is still Follow-up 4).

**Out of scope, with rationale (separate specs later):**
- `owner_id` on `Profile`/`TtsProfile`/`McpServer` + clone-from-template UX. Needs the
  `users` table this spec creates; doesn't need to block on it.
- `user_id` on `ChatSession`/`MemoryItem`, per-owner history filtering in
  `GET /v1/sessions`. Same dependency reasoning.
- `user_id` scoping of the livehost registry/session.
- Model/engine registry `enabled` + `stage=testing` fields and the filter that uses
  `users.can_use_testing` (column added now, consumed later).
- Email verification / password reset via email. Signup is username+password only;
  admin can reset a user's password directly from the Users page if needed.
- Splitting into two separate apps/entrypoints (distinct admin console vs. user
  console). This spec adds role-gated tabs to the *existing* single-page app
  (Component 7) but does not restructure it into two apps — that's Follow-up 4.
- Final visual identity (real logo asset, motion, tuned palette). Component 7 is a
  text-level rename onto the existing `styles.css` theme, not a new palette.

## Component 1 — `users` table

New SQLAlchemy models in `app/services/db/models.py`, alongside the existing
`ChatSession`/`ChatMessage` (async engine — this is relational data with a uniqueness
constraint and FK, not a fit for the KV-blob `SqliteBackedStore` used by
`Profile`/`TtsProfile`/`McpServer`/`SystemConfig`):

```python
class User(Base):
    __tablename__ = "users"
    id: Mapped[str]            # uuid4, pk
    username: Mapped[str]      # unique, not null
    password_hash: Mapped[str] # "pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>"
    role: Mapped[str]          # "admin" | "user"
    can_use_testing: Mapped[bool] = mapped_column(default=False)
    disabled: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime]
```

Password hashing: stdlib `hashlib.pbkdf2_hmac("sha256", password, salt, 600_000)` with a
random 16-byte salt per user (`os.urandom`), stored alongside iteration count in the
hash string so the cost factor can be raised later without breaking old hashes. No new
dependency — matches the codebase's existing preference for stdlib primitives
(`hmac.compare_digest` for the device token, commit `837809c`).

`app/services/auth/users.py` (new): `create_user`, `get_by_username`, `get_by_id`,
`verify_password`, `set_disabled`, `set_role`, `set_can_use_testing`, `list_users` — thin
async functions over the async engine, no caching layer (login/admin-list traffic is low
volume; unlike the config stores this isn't read on every request).

## Component 2 — signup / login / session / bootstrap

`app/api/routes/auth.py` changes:
- `POST /api/auth/signup {username, password}` → creates a `role="user"` account.
  Rejects if username taken. No email, no verification step.
- `POST /api/auth/login {username, password}` → `verify_password`; on success
  `request.session["user_id"] = user.id`, `request.session["role"] = user.role`
  (replaces today's single `session["authenticated"] = True`).
- `POST /api/auth/logout` → clears session, unchanged.
- `GET /api/auth/status` → `{authenticated, user_id, username, role, can_use_testing}`.
- `role="admin"` cannot be self-signed-up; only created via bootstrap or promoted by an
  existing admin (`PATCH /v1/users/{id}/role`, admin-only, added as part of Component 3's
  Users management route group).

Bootstrap (`app/main.py` lifespan, where config stores are already seeded): on startup,
if the `users` table is empty, create one admin account. Source of the initial
credentials, in order: `settings.admin_bootstrap_username` /
`admin_bootstrap_password` (new env-backed settings) if set; else fall back to reading
the legacy `settings.admin_password` (if still set) as the bootstrap admin's password
with username `"admin"`, so existing deployments don't lock themselves out on upgrade.
If neither is set, log a warning that no admin exists and the Users page must be used
(impossible without an admin — documented as a known chicken-and-egg case operators
must avoid by setting one bootstrap env var before first deploy).

## Component 3 — authorization guard

`AuthGuardMiddleware` (`app/core/auth_guard.py`) keeps its coarse job — "must be logged
in at all" — for the prefixes it already covers, but the allowlist now splits into two
groups, checked against `request.session.get("role")`:

- **Admin-only**: `/v1/system`, `/v1/models`, `/v1/users`, `/v1/devices` (admin overview
  endpoints), `/ui` sub-paths that render those pages.
- **Any authenticated user**: `/v1/conversation`, `/v1/livehost`, `/v1/profiles`,
  `/v1/mcp`, `/v1/tts_profiles`, `/v1/sessions`, `/v1/devices/mine` (a user's own device
  list/claim, kept separate from the admin `/v1/devices` overview).

A request to an admin-only prefix from a `role="user"` session gets `403`, not a
redirect to login (they *are* logged in — just not authorized).

New routes for user management (admin-only), backing the "Users management" page:
`GET /v1/users`, `PATCH /v1/users/{id}` (`{disabled?, role?, can_use_testing?}`),
`POST /v1/users/{id}/reset_password` (admin sets a new password directly — the
YAGNI-approved substitute for an email flow).

## Component 4 — periodic disabled/revoked re-check

`ConversationSession` (`app/services/conversation/session.py`) already runs an
idle-timeout watchdog loop per connection (landed with the Lugo idle-timeout work). This
spec adds a second, similarly-structured background check on the same loop cadence class
(~30s): if the session has a resolved `user_id`, re-query `users.disabled`; if it has a
resolved `device_id`, also re-query `devices.revoked`. On either becoming true, the
watchdog closes the connection (reusing the existing farewell/close-code path, not a
raw disconnect).

This only fires for sessions that *have* an identity to check. Browser sessions always
have `user_id` from the cookie. Device sessions have `user_id`/`device_id` only when
connected via a paired-device token (Component 5); a connection still using the legacy
shared `device_auth_token` has neither, and is not subject to this re-check — disabling
a user does not retroactively cut off devices that haven't been paired yet.

## Component 5 — `devices` table & pairing flow

```python
class Device(Base):
    __tablename__ = "devices"
    id: Mapped[str]           # uuid4, pk
    user_id: Mapped[str]      # fk users.id
    name: Mapped[str]         # user-supplied label at claim time
    serial: Mapped[str]       # stable hw identifier (MAC / chip id / machine-id), unique among non-revoked rows
    token_hash: Mapped[str]   # sha256 of the device's bearer token (opaque random secret, not derived from serial)
    created_at: Mapped[datetime]
    last_seen_at: Mapped[datetime | None]
    revoked: Mapped[bool] = mapped_column(default=False)
```

`serial` and `token_hash` are deliberately different fields: `serial` is a stable,
non-secret hardware identifier (a MAC address is guessable/broadcastable, so it must
never double as a credential); `token_hash` is the actual auth secret. This also means
re-pairing the same physical device produces a *recognizable* conflict instead of a
silent duplicate.

**Pairing flow** (device has a display or console log, no input needed — fits the
ESP32 OLED and the RPi client's stdout):

1. `POST /v1/devices/pair/init {serial}` (no auth) → server stores a short-lived
   (~10 min) in-memory pending-pairing entry keyed by a random 6-digit `code`, along
   with a `poll_token` and the `serial` (carried forward to the claim step below);
   returns `{code, poll_token}`. In-memory only —
   same pattern as the existing `livehost_registry` process-global dict
   (`app/services/livehost/registry.py`); losing it on restart just means the device
   retries `pair/init`.
2. Device displays `code`, polls `GET /v1/devices/pair/status?poll_token=...` every few
   seconds.
3. User, logged in, on the Devices page: "Add device" → enters `code` →
   `POST /v1/devices/pair/claim {code, name}` (any authenticated user). Server:
   - Looks up the pending entry by `code`; 404/expired if not found.
   - **If a non-revoked `Device` row already exists with this `serial`: reject with a
     clear error telling the user to revoke the existing device first.** (Chosen
     explicitly over auto-reclaim or silent duplicate rows — re-pairing always requires
     an explicit revoke, regardless of whether the claimer is the same user or not.)
   - Otherwise creates the `Device` row (`user_id` = claimer, generates the bearer
     token, stores only its hash), consumes the pending entry.
4. Device's next `pair/status` poll returns `{claimed: true, device_id, token}` and
   stores the token permanently (e.g. NVS on ESP32) for all future WS connects as
   `?device_token=<token>`.

**Devices pages:**
- Admin (`GET /v1/devices`, admin-only): all devices, owner, `last_seen_at`, revoke
  toggle.
- User (`GET /v1/devices/mine`, any authenticated user): only their own devices, revoke
  toggle, "Add device" claim UI.

## Component 6 — WS auth integration

`ws_authenticated()` (`app/core/auth_guard.py`) is extended to resolve an identity, not
just a boolean, and callers use the resolved identity to set `ConversationSession`'s
`user_id`/`device_id` (consumed by Component 4's watchdog):

```python
def resolve_ws_identity(websocket) -> WsIdentity | None:
    if websocket.session.get("user_id"):
        return WsIdentity(user_id=websocket.session["user_id"], device_id=None)
    token = websocket.query_params.get("device_token")
    if not token:
        return None
    if device := lookup_device_by_token_hash(sha256(token)):
        if device.revoked or device.owner.disabled:
            return None
        return WsIdentity(user_id=device.user_id, device_id=device.id)
    if settings.device_auth_token and token == settings.device_auth_token:
        return WsIdentity(user_id=None, device_id=None)  # legacy, unowned, temporary
    return None
```

Applied at all four WS routes that currently call the old boolean `ws_authenticated()`
— `conversation.py`, `stt.py`, `livehost.py` — plus `lugo.py`, which gets the call added
for the first time (Component 1's fix-while-touching-this-code item). A `None` result
closes the connection before `accept()`, same as today's "not authenticated" behavior.

The legacy shared-token branch is a deliberate, temporary carve-out: existing deployed
ESP32/RPi units keep working unmodified during rollout, but get none of the new
per-owner protections (Component 4's re-check, per-device revoke, "which user does this
belong to" visibility). Retiring it is a manual follow-up once the fleet has re-paired —
not scheduled automatically, not deleted as part of this spec.

## Component 7 — UI (Lugo branding, layout first)

`app/static/styles.css` already defines a coherent theme (`--bg-1`/`--bg-2` dark navy,
`--accent` teal, `--accent-2` amber, `--text`/`--muted`, Chakra Petch display +
IBM Plex Mono utility fonts) used consistently across every existing section. Per the
priority set for this round (layout/IA over visual polish), this spec does **not**
introduce a new palette or new CSS custom properties — it reuses the existing tokens
and existing component classes (`.model-row`, `.mini`, `.danger`, `.hint`, `.section`,
`.seg`) for the new Users/Devices UI, exactly as `mcp-servers.js`'s server list does
today. The only branding change in this round is text: every occurrence of "Speech
Text Transformer" in `index.html`/`login.html` (page `<title>`, `.app-title` header,
login card `<h1>`) becomes "Lugo" (page chrome) / "Lugo BOT" (browser tab titles). A
tuned palette/logo is Follow-up 5.

**Login/signup** (`app/static/login.html`, `app/static/js/auth.js`): one screen, one
card, a toggle between "Log in" and "Create account" instead of a separate page —
Login adds a `username` field (currently password-only); Create-account is
`username`/`password`/`confirm`, posting to the new `/api/auth/signup`. Wordmark
"Lugo" above the card.

```
┌─────────────────────────────┐
│            Lugo              │
│  ┌─────────────────────┐    │
│  │ Username             │    │
│  │ Password             │    │
│  │ [      Log in      ] │    │
│  │  New here? Create    │    │
│  │  account →            │    │
│  └─────────────────────┘    │
└─────────────────────────────┘
```

**App shell** (`app/static/index.html`, `js/sidebar-nav.js`): existing sidebar keeps
its current tabs (Chat/Livehost/STT/TTS/Models/MCP/System) and gains two more —
"Devices" (visible to every logged-in user) and "Users" (rendered only when
`GET /api/auth/status` reports `role: "admin"`, matching Component 3's guard so the
tab's visibility always matches what the backend actually allows). A divider separates
an "Quản trị" (admin) group from the rest — encodes the real role boundary, not
decoration:

```
┌──────────┬─────────────────────────────┐
│ Lugo     │  [page title]                │
│──────────│                              │
│ Chat     │                              │
│ Livehost │         [tab content]        │
│ Profiles │                              │
│ Devices  │                              │
│──────────│  ── Quản trị ──               │
│ Users    │  (chỉ hiện nếu role=admin)   │
│ Models   │                              │
│ MCP      │                              │
│ System   │                              │
└──────────┴─────────────────────────────┘
```

**Users page** (admin-only; new `js/users.js` module, following the existing per-tab
module pattern e.g. `profiles.js`/`mcp-servers.js`): table of all users — username,
role, `can_use_testing` checkbox, status (Active/Disabled), and a row action menu
(Disable/Enable, change role, toggle testing access, reset password). Backed by
Component 3's `/v1/users` routes.

```
Users                                      [+ Create user]
┌────────────┬───────┬─────────┬──────────┬─────────┐
│ Username    │ Role  │ Testing │ Status   │ Actions  │
├────────────┼───────┼─────────┼──────────┼─────────┤
│ toan        │ admin │  —      │ Active   │ ⋯        │
│ linh        │ user  │  ☑      │ Active   │ ⋯        │
│ khanh       │ user  │  ☐      │ Disabled │ ⋯        │
└────────────┴───────┴─────────┴──────────┴─────────┘
```

**Devices page** (new `js/devices.js`): every logged-in user sees "Thiết bị của tôi"
(their own devices, via `/v1/devices/mine`) with a revoke action and a "Thêm thiết bị"
button that opens a modal for entering the pairing `code` shown on the physical device
plus a display name (Component 5's `pair/claim`). Admins additionally see a second,
read-only-except-revoke table "Tất cả thiết bị" (`/v1/devices`) with an owner column.

```
Devices                                    [+ Thêm thiết bị]
── Thiết bị của tôi ──
┌───────────┬───────────┬───────────┬─────────┐
│ Name       │ Serial     │ Last seen  │ Action  │
└───────────┴───────────┴───────────┴─────────┘

── Tất cả thiết bị (chỉ admin) ──
┌───────────┬────────┬───────────┬───────────┬─────────┐
│ Name       │ Owner  │ Serial     │ Last seen  │ Action  │
└───────────┴────────┴───────────┴───────────┴─────────┘
```

## Migration / rollout notes

- Existing single-page app's login form (`login.html`) needs a username field added
  (currently password-only) and a signup toggle (Component 7); `js/auth.js`'s global
  401→redirect behavior is unaffected.
- No data migration for existing config stores — `Profile`/`TtsProfile`/`McpServer`
  rows stay ownerless (global templates) until the follow-up ownership spec runs.
- `settings.admin_password` and `settings.device_auth_token` both remain valid, read as
  legacy fallbacks (bootstrap admin creation and the WS legacy branch respectively) —
  neither is removed by this spec.

## Testing plan

- Unit: password hash/verify round-trip, including wrong-password and legacy-migration
  cases; pending-pairing expiry; serial-conflict rejection on `pair/claim`.
- Integration: signup → login → session carries role; admin-only route 403s for a
  `role=user` session; full pair/init → claim → status → WS-connect-with-new-token
  round trip; disabled-user's open WS session is closed within the watchdog interval in
  a test that fast-forwards the check (don't sleep 30s in the test).
- Manual: confirm `/v1/lugo/stream` now rejects an unauthenticated connect (previously
  it didn't); confirm the "Users" sidebar tab is absent for a `role=user` session and
  present for `role=admin`; confirm the full pairing UX end-to-end with a real ESP32 or
  the RPi client (device shows code → claim on Devices page → device connects with its
  new token).

## Follow-ups (separate specs, not built here)

1. Resource ownership (`owner_id` on `Profile`/`TtsProfile`/`McpServer`, clone-from-
   template UX) + model/engine registry `enabled`/`stage=testing` gated by
   `can_use_testing`.
2. `user_id` scoping of `ChatSession`/`MemoryItem`/session history listing.
3. `user_id` scoping of the livehost registry/session.
4. Full frontend split into a separate admin console and user console app/entrypoint
   (this spec only adds role-gated tabs within the existing single-page app).
5. Final Lugo visual identity pass (real logo, motion, tuned palette) on top of
   Component 7's placeholder styling.
