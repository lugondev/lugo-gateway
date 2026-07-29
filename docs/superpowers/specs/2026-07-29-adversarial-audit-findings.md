# Adversarial security audit — 2026-07-29

Active/adversarial audit of the auth code merged in `c8567ae` (the critical-authz
branch) plus adjacent surfaces. Four Opus agents attacked the middleware,
session/WS ownership, the containment/admin/input layer, and the device/pairing
surface — trying to BREAK the code, not confirm it. Every finding below was
reproduced or traced to file:line; negative results (vectors that held) are at
the end.

**Two defenses held up under attack** (stated first, they are real wins):
- The middleware default-deny floor resisted traversal, `%2f`, dot-segments,
  static-escape, bearer→admin escalation, and enumeration oracles.
- `ws_session_owner_denied`'s allow-list (`via_login`) resisted flag spoofing,
  cookie/token forgery, and null-scope collision.

The holes are at the *edges* the guards don't cover, and in surfaces the guards
were never wired into.

## Root causes (most findings cluster into three)

**R1 — The guard classifies on `request.url.path`, which is NOT what the router
dispatches on.** `request.url.path` truncates at `#`/`?`; the router uses
`scope["path"]` (via `get_route_path`, which also strips `root_path`). Any
divergence between the two strings is a bypass. Fix once: classify on
`starlette._utils.get_route_path(request.scope)`, reject `#`/`?`/dot-segments,
make carve-outs exact-match, and check admin-prefixes before user-prefixes.

**R2 — The profile surface is an ungated master key.** A `Profile` carries
`mcp_servers` (URL+headers), `llm.api_key`, `system_prompt`, and drives
`ref_audio_path`; `?profile=` is resolved with no `_visible` check on any
conversation surface, and `mcp_servers`/`owner_id` are not role-gated on write.
Fix: `_visible`-check `?profile=`/`?tts_profile=` at every use site; force
`mcp_servers=[]` for non-admin writes; drop the `or profile.owner_id` create-time
fallback.

**R3 — The per-row skip in `_ensure()` makes a malformed row invisible but not
absent** — it still holds the PK, so `get(name) is None` reports the name free
and it becomes claimable/overwritable. Fix: track skipped keys; treat them as
existing in the routes and in `seed_default_servers`.

## Findings (ranked, deduped across agents)

### CRITICAL

**C1 — MCP admin gate (Task 6) fully bypassable via `POST /v1/profiles`
`mcp_servers[]`.** `Profile.mcp_servers` is a user-writable field
(`profiles/models.py:57`), not role-gated in `create_profile`/`update_profile`
(`profiles.py:119,143`), and `_build_tool_registry` merges it into the same fetch
path with the caller's URL+headers, profile entries winning
(`session.py:70-91`). A non-admin creates a profile with an arbitrary
`mcp_servers[].url` (e.g. `http://169.254.169.254/...?`) and connects
`?profile=` → gateway fetches it and reflects the body into the LLM turn. Same
SSRF-with-reflection Task 6 was written to close, through a door Task 6 didn't
cover. **Branch gap (Task 6).** Fix: gate `mcp_servers` on admin in the profile
routes.

**C2 — Profile IDOR via `?profile=` / `?tts_profile=`.** No `_visible` check on
`conversation.py:148,354,370`, `lugo.py:47,54`, `livehost.py:141,159`,
`stt.py:175`, `services/health.py:35,39`, `session.py:209`. `conversation.py`
does not even import `_visible`. Any signed-up user names another user's private
profile and runs on their `llm.api_key`, `system_prompt`, and private
`mcp_servers` (with auth headers); the WS `session_started.active_tools` returns
the victim's private tool names directly. Global 409-on-create and the
"profile not found" warning are existence oracles for enumeration. **Pre-existing
(HIGH in the original audit, deferred); adversary proved CRITICAL.** Fix:
`_visible(profile, caller_id)` at every resolution site.

**C3 — Device-pairing hijack.** 6-digit code (`pairing.py:33`), 600s TTL, no
rate limit anywhere; `pair/claim` binds the pairing to whoever submits the code
with no proof of hardware ownership (`devices.py:40-55`). Attacker brute-forces
the code, claims the victim's device onto the attacker's account; the victim's
conversations are then stored under the attacker's `user_id` and readable via
`/v1/sessions`. Reproduced end-to-end reading a victim message. **Pre-existing
(MEDIUM in original audit); adversary proved CRITICAL.** Fix: rate-limit + burn
code after N attempts, widen entropy, tie the claim to the init session.

### HIGH

**H1 — `#`-truncation defeats `_STATIC_ALLOWLIST` → whole static mount served
unauthenticated.** Guard matches `request.url.path` (`auth_guard.py:133`), which
drops everything from the first `#`; uvicorn decodes `%23`→`#` before routing.
`curl --path-as-is 'https://host/static/login.html%23/../index.html'` → guard
sees `/static/login.html` (allowlisted) → serves the admin console + every JS
module with no credentials. Verified 200 with admin HTML. **Branch code (R1).**

**H2 — Fleet-token + `or profile.owner_id` fallback creates sessions owned by an
arbitrary victim.** `conversation.py:224`, `session.py:312`, `livehost.py:236`
use `caller_id or (profile.owner_id ...)`; `profile.owner_id` is attacker-chosen
(just a profile name). A `user_id=None` identity (fleet token) creates a row
owned by the named profile's owner — the code comment claiming "created ownerless
by construction" is false. Write-only content injection into a victim's history,
billed to their key. **Branch code (R2).** Fix: drop the `or profile.owner_id`
fallback for null identities.

**H3 — `http_tts` reads ref audio synchronously on the event loop + unbounded
upload → full-worker DoS.** `http_tts_provider.py:168` `Path(...).read_bytes()`
then `base64.b64encode` on the coroutine, no `to_thread` (the other 5 providers
wrap it); `POST /v1/tts/reference-audio` has no size cap (`tts.py:58`). One
authenticated user uploads a large contained file, points `ref_audio_path` at it,
and freezes the single worker. Reproduced: 0 heartbeat ticks during the blocking
read. Fix: `to_thread` the read+encode; cap the upload.

**H4 — Malformed row is invisible therefore claimable.** The per-row skip
(`config_store.py:79-89`) hides a row that still holds its PK; `_put` blind-
overwrites (`:135-143`). `POST /v1/tts/profiles {"name":"<victim>"}` sees
`get()==None` → no 409 → overwrites the victim's row, `owner_id` becomes the
attacker; `PUT` skips `_can_write` because `existing is None`. `seed_default_servers`
(`main.py:129`) silently replaces a malformed `basic-tools` row with the preset.
**Branch code (R3).** Fix: track skipped keys, treat as existing.

**H5 — livehost WS + 3 HTTP control routes are unauthorized (IDOR).**
`livehost.py:137` takes `?session_id=` with no `ws_session_owner_denied`;
`register()`/`unregister()` overwrite/orphan a victim's live ingestor. The three
HTTP routes (`:97-119`) have no owner check: connect/disconnect/status another
user's TikTok session by uuid. **Pre-existing/deferred; confirmed by 3 agents.**
Fix: `ws_session_owner_denied` in the WS route; `_scope_user_id` on the 3 HTTP
routes.

### MEDIUM

**M1 — Path-param shadowing of admin handlers.** `_matches` treats
`/v1/devices/mine` as covering `/v1/devices/mine/...`; `POST /v1/devices/mine/revoke`
matches the user carve-out and reaches the admin `revoke_any_device(device_id="mine")`.
Same for `PATCH/DELETE /v1/model_registry/options` (+ `#`-trick variants). Today
all 404 (no matching row), but the gate is broken. **Branch code (R1).** Fix:
admin-first ordering + exact-match carve-outs.

**M2 — `OPTIONS` exempt from the guard → unauthenticated route/method
enumeration.** `auth_guard.py:127-128` returns early for every `OPTIONS`;
a plain `OPTIONS` (no CORS preflight headers) reaches the router → `405 Allow:...`
maps the admin surface. **Branch code.** Fix: only exempt genuine preflights.

**M3 — `root_path` divergence fails admin prefixes open.** Under `--root-path /gw`
the guard sees `/gw/v1/system/config`, matches nothing, falls to the user floor;
the router strips `/gw` and dispatches the admin route. Latent (no root_path in
current deploy configs). **Branch code (R1).** Fix: classify on `get_route_path`.

**M4 — `PATCH /v1/mcp/servers/{name}/enabled` ungated.** The one mutating MCP
route Task 6 missed (`mcp.py:101-108`); `_can_write` lets a user-owned row's owner
re-enable it, and `_build_tool_registry` reads `mcp_server_store.list()`
unfiltered, injecting that row's tools into every user's turn. **Branch gap.**
Fix: `_require_admin`; null-out legacy user-owned rows.

**M5 — Unbounded `TTSRequest.text` and reference upload.** No `max_length` on
`text` (`schemas/tts.py:7`); `/v1/tts/synthesize` no longer segments. Fix:
`max_length` + upload cap + body-limit middleware.

### LOW

- MCP pool keys clients/cache by URL only, ignoring headers → cross-tenant header
  reuse once users can register URLs (`pool.py:34-57`).
- Clone routes skip `check_model_allowed` (`tts_profiles.py:114`, `profiles.py:195`).
- `ref_audio_path: ""` is a 422 instead of a no-op (`schemas/tts.py:30-33`).
- Unbounded `base64.b64decode` in model_service (token-gated) (`routes_tts.py:62`).
- Open unauthenticated signup makes "logged-in user" a two-request barrier —
  raises the practical severity of every user-level finding.
- `auth_enabled` fail-open is silent when `APP_ENV` is unset (default `"dev"`).

## Vectors that HELD (informed silence)

Containment `contains()` (30+ crafted inputs incl. symlink-inside-pointing-out,
prefix-sibling, macOS case, NUL, long paths — all correctly rejected; all 6
provider read sites use the validated string verbatim; OmniVoice CLI cwd-divergence
proven unexploitable). `ws_session_owner_denied` allow-list (via_login single
source, bearer/device/fleet cannot reach the admin bypass, DB-role read
uninfluenceable). Null/empty scope collision (no create path stores `""`).
events.py job + session channels (miss → 404, owner from server-side id).
Memory cross-user (None→"" shared bucket, equality-filtered). Bearer↔cookie
cross-forgery (distinct salts). `current_role()` "admin" fallback (login writes
both keys). Path normalization other than `#`/`?`. model_service decode target
(uuid4, O_EXCL 0o600, unlinked, token-gated). `had_rows` under concurrency.
synthesize bytes/media-type contract.
