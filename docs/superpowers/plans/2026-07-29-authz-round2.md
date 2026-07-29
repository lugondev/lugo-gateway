# Authz Round 2 — Adversarial-Audit Remediation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Close the ~13 findings from the 2026-07-29 adversarial audit, grouped by root cause, without regressing the fixes the previous branch shipped.

**Architecture:** Three root causes plus standalone criticals. R1 = the guard classifies on `request.url.path` (truncates at `#`, ignores `root_path`) instead of the router's dispatch path. R2 = the profile surface is an ungated master key (`?profile=` unchecked, `mcp_servers` not role-gated, `owner_id` create-time fallback). R3 = the per-row skip in `_ensure()` makes a malformed row invisible-but-present, hence claimable.

**Tech Stack:** Python 3.12, FastAPI/Starlette, pydantic v2, pytest + pytest-asyncio, httpx MockTransport.

## Global Constraints

- Run tests with `.venv/bin/pytest` from the repo root (symlinked venv in the worktree).
- Baseline: `.venv/bin/pytest tests/unit tests/integration -q` → **1653 passed, 1 failed**. The one failure is `tests/integration/test_stt_ws.py::test_ws_stream_partial_then_final_then_done`, pre-existing on untouched main — do NOT fix it. A single `StarletteDeprecationWarning` from `fastapi.testclient` is pre-existing repo-wide.
- **Test hermeticity:** never depend on an optional pip extra (`vieneu`/`omnivoice`, not in `dev`) or a gitignored `models/` file. `tests/conftest.py`'s `_hermetic` blanks admin passwords → `auth_enabled` False → middleware short-circuits; `tests/unit/test_auth_guard_default_deny.py` has a `_with_password` fixture, `tests/unit/test_conversation_authz.py` an `_as_user` login helper — reuse them.
- **Every security fix needs an adversarial regression test** — the exact exploit from the findings doc, asserting it now fails (403/404/422 or degraded), plus that the legitimate case still works. Assert on message/detail text, not just status codes.
- `TestClient.stream(...)` deadlocks on never-closing SSE/WS — use `websocket_connect` + one `receive_json()`, or call the route coroutine directly.
- Full findings with file:line and reproductions: `docs/superpowers/specs/2026-07-29-adversarial-audit-findings.md` — every task cites its finding IDs; read that section before implementing.
- Commit as the repo default identity.

---

### Task 1 (R1): Classify the guard on the router's dispatch path

**Closes:** H1 (`#`-truncation static bypass), M1 (path-param shadowing of admin handlers), M2 (OPTIONS enumeration), M3 (root_path fail-open).

**Files:**
- Modify: `apps/api_gateway/app/core/auth_guard.py`
- Test: `tests/unit/test_auth_guard_path_classification.py`

**Background:** `dispatch` reads `path = request.url.path` (`auth_guard.py:133`). `request.url.path` drops everything from the first `#`/`?`, and uvicorn decodes `%23`→`#` before routing, so `/static/login.html%23/../index.html` is seen by the guard as `/static/login.html` (allowlisted) but dispatched by the router as the truncated-then-normalized real path. The router classifies on `starlette._utils.get_route_path(scope)` (which also strips `root_path`). The guard must classify on the SAME string.

**Fixes, all in `dispatch`/`_matches`:**
1. Replace `path = request.url.path` with `from starlette._utils import get_route_path` / `path = get_route_path(request.scope)`. This fixes H1 and M3 at once (no `#` truncation, root_path stripped consistently with the router).
2. Defense in depth: if the raw `request.url.path` (or `scope["raw_path"]`) contains `#` or a `%23`, or a `.`/`..` segment, deny — a request that needs those to classify differently is hostile. Add this as an explicit reject before classification.
3. Make the user carve-outs that sit *inside* admin prefixes EXACT-match, not prefix-match, so `PATCH /v1/model_registry/options` and `POST /v1/devices/mine/revoke` fall through to the admin rule. Introduce a `_USER_EXACT` frozenset for `/v1/usage/me`, `/v1/model_registry/options`, `/v1/model_registry/defaults`, `/v1/devices/mine`, `/v1/devices/pair/claim` (verify each against the routers — some legitimately need subpaths, e.g. `/v1/devices/mine` may have `/v1/devices/mine/...`; for those keep prefix but ALSO check admin-first per #4). Read each carve-out's real routes before deciding exact-vs-prefix.
4. Check `_ADMIN_PREFIXES` BEFORE `_USER_PREFIXES` for any path that matches both, so a user carve-out can never shadow an admin handler. The cleanest form: build one ordered classification that resolves the most-specific rule; if you keep two tuples, check admin first and let the exact user carve-outs be the explicit exceptions.
5. Narrow the OPTIONS exemption: only skip the guard when the request carries `Access-Control-Request-Method` (a genuine CORS preflight). A plain `OPTIONS` must be classified like any other method.

**Adversarial regression tests (all must fail before the fix):**
- `curl`-equivalent: a request whose `url.path` truncates to an allowlisted static path but whose real path is `index.html` → denied (401). Drive it by constructing the scope/path the way `test_auth_guard_default_deny.py` drives the client, or by unit-testing the classification function on the raw vs route path.
- `POST /v1/devices/mine/revoke` as a non-admin → 403/handled by admin rule, not the user carve-out.
- `PATCH /v1/model_registry/options` as a non-admin → 403.
- Plain `OPTIONS /v1/users` (no `Access-Control-Request-Method`) as anonymous → not served (401/403), while a real preflight (with the header) still passes.
- Existing `test_auth_guard_route_coverage.py` and `test_auth_guard_default_deny.py` must still pass.

- [ ] Step 1: failing tests. Step 2: run, confirm red. Step 3: implement. Step 4: green. Step 5: full suite (this touches the core guard — any new failure is a real signal). Step 6: commit `fix(auth): classify the guard on the router dispatch path, not request.url.path`.

---

### Task 2 (R2a): `_visible`-check `?profile=` / `?tts_profile=` at every use site

**Closes:** C2 (profile IDOR — cross-user LLM key / system prompt / MCP).

**Files:**
- Modify: `apps/api_gateway/app/api/routes/conversation.py` (`:148`, `:354`, `:370`), `apps/api_gateway/app/api/routes/lugo.py` (`:47` via `_resolve`, `:54`), `apps/api_gateway/app/api/routes/livehost.py` (`:141`, `:159`), `apps/api_gateway/app/api/routes/stt.py` (`:175`), `apps/api_gateway/app/services/health.py` (`:35`, `:39`), and re-check in `apps/api_gateway/app/services/conversation/session.py:209`.
- Test: `tests/unit/test_profile_idor.py`

**Background:** Profiles and TTS profiles are `owner_id`-scoped; every CRUD route enforces `_visible()` (`profiles.py`) / the tts-profile equivalent (`tts_profiles.py`). Every *consumer* resolves by bare name with no check. `conversation.py` doesn't even import `_visible`. `memories.py:_require_visible` is the pattern to mirror.

**Fix:** after resolving a profile/tts-profile from a client-supplied name, treat it as not-found unless `_visible(profile, caller_id)` (caller_id = `current_user_id(request)` on HTTP, `identity.user_id` on WS). A template (`owner_id is None`) stays visible to all, matching `_visible`'s existing semantics — verify that's what `_visible` does before relying on it. On the WS paths, emit the same "profile not found" shape already used so existence isn't leaked differently for visible-vs-owned. Factor a small shared helper rather than copy-pasting the predicate 8 times.

**Watch:** the 409-on-create global-namespace oracle and the WS "profile 'X' not found" warning are separate enumeration oracles (findings doc C2) — out of scope for this task's fix but note them in the report; do not make the not-visible case distinguishable from the not-exists case.

**Adversarial regression tests:** user B, given the name of user A's private profile, gets not-found (not A's system prompt / tools / key) on `chat`, the conversation WS, lugo, and via `?tts_profile=`. A template profile still works for everyone. The owner still gets their own profile.

- [ ] Steps: failing tests → red → implement shared helper + wire all sites → green → conversation/lugo/livehost/stt suites → commit `fix(profiles): reject cross-user ?profile=/?tts_profile= at use time`.

---

### Task 3 (R2b): Gate `profile.mcp_servers` on admin; drop the `owner_id` create fallback

**Closes:** C1 (MCP SSRF gate bypassed via profiles), H2 (fleet-token owner_id fallback creates victim-owned sessions).

**Files:**
- Modify: `apps/api_gateway/app/api/routes/profiles.py` (`create_profile` `:140`, `update_profile` `:159`, `clone_profile` `:195`), `apps/api_gateway/app/api/routes/conversation.py:224`, `apps/api_gateway/app/services/conversation/session.py:312`, `apps/api_gateway/app/api/routes/livehost.py:236`.
- Test: `tests/unit/test_profile_mcp_gate.py`, add to `tests/unit/test_conversation_authz.py`

**Background — C1:** `Profile.mcp_servers` (`profiles/models.py:57`) is a user-writable field, and `_build_tool_registry` (`session.py:70-91`) merges it into the same fetch path as admin-managed MCP rows, with profile entries winning, using the entry's URL+headers. So Task 6 of the previous branch (admin-only `/v1/mcp/servers`) is bypassed: a non-admin sets `mcp_servers` on their own profile and gets the SSRF-with-reflection primitive.

**Fix C1:** in `create_profile`/`update_profile`/`clone_profile`, force `mcp_servers` to `[]` (create/clone) or the existing stored value (update) unless `current_role(request) == "admin"`. Mirror the one-line role gate `mcp.py` uses. A non-admin PUT that tries to change `mcp_servers` must not persist the change (silently drop or 403 — pick one, state which, and test it).

**Background — H2:** `caller_id or (profile.owner_id if ...)` at the three create sites means a `user_id=None` identity (fleet token) creates a row owned by the *named profile's* owner, not ownerless. The code comment claims "created ownerless by construction" — make that true.

**Fix H2:** drop the `or profile.owner_id` fallback — use `caller_id` / `cfg.identity_user_id` / `identity.user_id` directly (may be `None` → ownerless, which is correct for the fleet/dev identity). Update the now-accurate comment.

**Adversarial regression tests:** non-admin `POST /v1/profiles` with `mcp_servers=[{url:...}]` → stored profile has empty `mcp_servers` (or 403); admin can still set it. A fleet-token WS session naming a victim's profile creates an ownerless row (not victim-owned) — assert the created row's `user_id` is None, not the profile owner's. Admin-created template `mcp_servers` still load for everyone (they're global rows, unaffected).

- [ ] Steps: failing tests → red → implement → green → profile + conversation-authz suites → commit `fix(profiles): admin-gate mcp_servers and stop owner_id session-ownership fallback`.

---

### Task 4 (R3): Make a skipped malformed row un-claimable

**Closes:** H4 (invisible-but-present row is claimable/overwritable).

**Files:**
- Modify: `apps/api_gateway/app/services/db/config_store.py` (`_ensure` `:79-89`, `_put` `:135-143`), and the callers that decide new-vs-existing: `apps/api_gateway/app/api/routes/tts_profiles.py` (`:62`, `:84`), `apps/api_gateway/app/api/routes/profiles.py` (`:141`, `:161`), `apps/api_gateway/app/main.py:129` (`seed_default_servers`).
- Test: `tests/unit/test_config_store_claimable.py`

**Background:** The per-row skip (correct for availability) hides a row that still holds its PK. `get(name) is None` then reports the name free, so `POST` creates-over-it (owner becomes attacker) and `PUT` skips `_can_write` (because `existing is None`), and `seed_default_servers` replaces a malformed preset row with defaults.

**Fix:** track skipped keys on the store (e.g. `self._unreadable: set[str]`, populated in `_ensure`'s except branch). Expose a way to ask "does this name exist even if unreadable" (a method or have `_put`/`upsert` and the routes consult it). A name in `_unreadable` must be treated as EXISTING: `POST` → 409, `PUT`/`DELETE` → a clear 5xx/409 ("row exists but is unreadable"), `seed_default_servers` → skip (do not overwrite). Do NOT silently expose the row's content — it stays unreadable; the point is it can't be claimed.

**Adversarial regression test (round-trip through `_ensure`, not `upsert`):** write a malformed row straight to the DB (the `_write_raw_row` helper pattern in `tests/unit/test_tts_profile_store.py`), then: `get()` still returns None (skip preserved), but `POST /v1/tts/profiles` with that name → 409 not overwrite, and the raw `row.data` is byte-identical afterwards. Also: `seed_default_servers` does not overwrite a malformed `basic-tools` row.

- [ ] Steps: failing tests → red → implement → green → store + tts_profile + mcp suites → commit `fix(config-store): treat unreadable rows as existing so they can't be claimed`.

---

### Task 5 (C3): Harden device pairing

**Closes:** C3 (pairing-code brute-force → device hijack → cross-user conversation read).

**Files:**
- Modify: `apps/api_gateway/app/services/auth/pairing.py`, `apps/api_gateway/app/api/routes/devices.py` (`pair_init`, `pair_claim`, `pair_status`).
- Test: `tests/unit/test_pairing_hardening.py`

**Background:** 6-digit code, 600s TTL, no rate limit; `pair/claim` binds the pairing to whoever submits the code, no proof of hardware ownership. Brute-force → victim's device is claimed onto the attacker's account → victim's conversations stored under attacker's `user_id`.

**Fix (all three):**
1. Burn the code after a small number of failed claim attempts (e.g. 5) — track attempts per pending pairing, delete/lock it when exceeded. This defeats the 1e6 brute force within the TTL.
2. Widen the code entropy meaningfully (longer code, or alphanumeric) OR require the claimer to also present a value only the initiating side knows — decide and justify. Burning after N attempts is the primary defense; entropy is defense in depth.
3. Rate-limit `pair/claim` (and ideally `pair/init`) per IP/session — even a coarse in-process limiter. State the limiter's scope and its multi-worker caveat (process-local) in the report.

**Adversarial regression test:** simulate repeated wrong-code `pair/claim` → the code is burned after N attempts (further claims with the *correct* code fail). A single correct claim within the limit still pairs. Confirm a claimed device is bound to the claimer only through the intended flow.

- [ ] Steps: failing tests → red → implement → green → devices/pairing suites → commit `fix(pairing): rate-limit and burn brute-forced pairing codes`.

---

### Task 6 (H3 + M5): http_tts off-loop read + input size caps

**Closes:** H3 (event-loop DoS via synchronous ref-audio read + unbounded upload), M5 (unbounded `TTSRequest.text`).

**Files:**
- Modify: `apps/api_gateway/app/services/tts/providers/http_tts_provider.py:163-169`, `apps/api_gateway/app/api/routes/tts.py:58-66` (`reference-audio` upload), `apps/api_gateway/app/schemas/tts.py:7` (`text` bound).
- Test: `tests/unit/test_http_tts_offloop.py`, add to the tts schema tests.

**Fix:**
1. Wrap the `Path(...).read_bytes()` + `base64.b64encode` in `http_tts_provider._render_wav` in `asyncio.to_thread`, matching the other five providers.
2. Cap the `/v1/tts/reference-audio` upload: stream to a temp file with a byte counter (or check `Content-Length` / read with a cap) and reject over a few MB. Also give ref files a prune-eligible name + TTL (finding H3 tail / findings doc M5).
3. `Field(..., min_length=1, max_length=10_000)` on `TTSRequest.text` (pick a sane cap; state it).

**Regression tests:** the read is off-loop (assert the provider awaits `to_thread`, or a structural test); an oversized upload → 413/422; `text` over the cap → 422.

- [ ] Steps: failing tests → red → implement → green → tts suites → commit `fix(tts): off-load ref-audio read and bound upload/text sizes`.

---

### Task 7 (M4 + H5): MCP enabled gate + livehost ownership

**Closes:** M4 (`PATCH /v1/mcp/servers/{name}/enabled` ungated), H5 (livehost WS + 3 HTTP control routes IDOR).

**Files:**
- Modify: `apps/api_gateway/app/api/routes/mcp.py:101-108` (`set_server_enabled`), `apps/api_gateway/app/api/routes/livehost.py` (`:97-119` the 3 HTTP routes, `:130-137` the WS route + `register`).
- Test: `tests/unit/test_mcp_enabled_gate.py`, `tests/unit/test_livehost_authz.py`

**Fix M4:** add `_require_admin(request)` as the first line of `set_server_enabled`. Separately, null-out (or delete) any legacy user-owned `config_mcp_servers` rows via a one-time idempotent startup migration (there are none in the live DB today per the audit, but a legacy row is the precondition — mirror the repo's existing startup-migration precedent). State whether you did the migration or judged it unnecessary.

**Fix H5:** store the owning `user_id` on `LivehostSession` at `register()` time; the 3 HTTP routes (`connect`/`disconnect`/`status`) 404 unless `current_user_id(request)` owns it (admins unscoped, mirror `_scope_user_id`); the WS route calls `ws_session_owner_denied(session_id, identity)` before `register()`, mirroring `conversation.py`/`lugo.py`.

**Adversarial regression tests:** non-admin `PATCH .../enabled` on a template → 403; user B `POST /v1/livehost/<A's id>/disconnect` → 404; user B WS `?session_id=<A's id>` → error+close, and A's registry entry is NOT overwritten.

- [ ] Steps: failing tests → red → implement → green → mcp + livehost suites → commit as two commits (`fix(mcp): admin-gate enabled toggle` + `fix(livehost): scope session control to the owner`).

---

### Task 8 (LOW batch): small hardening

**Closes:** MCP pool header-keying, clone `check_model_allowed`, `ref_audio_path:""` no-op, model_service decode cap.

**Files:** `apps/api_gateway/app/services/mcp/pool.py:34-57`, `apps/api_gateway/app/api/routes/tts_profiles.py:114`, `apps/api_gateway/app/api/routes/profiles.py:195`, `apps/api_gateway/app/schemas/tts.py:30-33`, `apps/model_service/app/routes_tts.py:62`.
- Test: extend existing files.

**Fixes:** key `McpConnectionPool` clients/cache on `(url, frozenset(headers.items()))`; run `check_model_allowed` in both clone routes; `ref_audio_path == ""` → return `v` (no-op) in the `TTSRequest` validator; bound `base64.b64decode` size in model_service before decode. Each is small and independent; one commit per fix or a single batched commit — your call, tests for each.

- [ ] Steps per fix; commit `fix(security): low-severity hardening from the adversarial audit`.

---

### Task 9: Full verification + adversarial re-check

**Files:** none (verification).

- [ ] Full suite `.venv/bin/pytest tests/unit tests/integration -q` → all pass except the one pre-existing `test_stt_ws.py` failure.
- [ ] Manual, auth enabled, real DB, no/low creds — re-run each exploit from the findings doc and confirm it now fails: the `#`-truncation static fetch (401), a non-admin `?profile=<other>` (not-found), a non-admin profile with `mcp_servers` (empty/403), the claimable-row `POST` (409), a wrong-code pairing brute (burned). Record commands + outcomes to `.superpowers/sdd/2026-07-29-authz-round2/final-verification.md`.

---

## Self-Review
- H1/M1/M2/M3 → Task 1 ✓; C2 → Task 2 ✓; C1/H2 → Task 3 ✓; H4 → Task 4 ✓; C3 → Task 5 ✓; H3/M5 → Task 6 ✓; M4/H5 → Task 7 ✓; LOWs → Task 8 ✓; verification → Task 9 ✓.
- Root-cause grouping: R1=Task1, R2=Tasks 2+3, R3=Task4. Standalone criticals: C3=Task5.
- Every task carries an adversarial regression test reproducing its finding.
