# Structure Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Act on the 2026-07-29 structure audit — hygiene fixes, repo-root path resolution, mechanical refactors (import hoisting, schemas centralization, test-dir mirroring), and the livehost→ConversationSession unification — WITHOUT changing runtime behavior.

**Architecture:** Ordered safe→risky. Every task is behavior-preserving; the full test suite is the guard. No feature changes.

**Tech Stack:** Python 3.12, FastAPI/Starlette, pydantic v2, pytest.

## Global Constraints

- Run tests with `.venv/bin/pytest` from the repo root (symlinked venv in the worktree).
- Baseline: `.venv/bin/pytest tests/unit tests/integration -q` → **1751 passed, 1 failed**. The one failure is `tests/integration/test_stt_ws.py::test_ws_stream_partial_then_final_then_done`, pre-existing on untouched main — do NOT fix it. A single `StarletteDeprecationWarning` from `fastapi.testclient` is pre-existing repo-wide.
- **Behavior-preserving only.** No endpoint contract change, no new feature, no security-relevant behavior change (the auth/pairing/mcp fixes from the two authz rounds must stay exactly as-is). If a refactor would change observable behavior, STOP and flag it.
- `.venv/bin/python -c "import app"` resolves to the MAIN repo via an editable install — verify with `.venv/bin/pytest` (worktree-relative pythonpath), not ad-hoc python.
- Commit ONLY the task's files with explicit `git add <path>` — never `git add -A`.
- Full findings with file:line: `docs/superpowers/specs/2026-07-29-structure-audit-findings.md` — each task cites its finding IDs; read that section first.
- Commit as the repo default identity.

---

### Task 1 (hygiene B2/B3/B5/B6): repo cleanup

**Closes:** B2 (artifacts/ not gitignored), B3 (empty scaffold dirs), B5 (unbounded .bak), B6 (stray root log).

**Files:** `.gitignore`; delete `apps/api_gateway/data/`, `apps/api_gateway/artifacts/`, `apps/api_gateway/models/`, `uvicorn-8000.log`; the root `data/*.bak-*` handling.

**Note:** the dirs/files to delete are gitignored/untracked (except confirm), so deletion is a working-tree `rm`, not a `git rm`. Do NOT delete the live root `data/app.db` or root `artifacts/` contents (those are real runtime data) — only the stale/duplicate `apps/api_gateway/{data,artifacts,models}` scaffold and the stray root log.

- [ ] Step 1: add a bare `artifacts/` line to `.gitignore` (covers root + any per-app copy), next to the existing `data/`/`models/` lines. Verify `git check-ignore artifacts apps/api_gateway/artifacts` now both report ignored.
- [ ] Step 2: `rm -rf apps/api_gateway/data apps/api_gateway/artifacts apps/api_gateway/models` (all stale duplicates of the root dirs; the `app.db` there is the stale 32K Jul-23 copy — confirm it's NOT the live one first with `ls -la`). `rm -f uvicorn-8000.log`.
- [ ] Step 3: move the root `data/app.db.bak-*` files into `data/backups/` (create it; it's under the already-ignored `data/`), so they're grouped. Do not delete them (they're migration backups) — just group. Confirm `data/app.db` (live) is untouched.
- [ ] Step 4: `git add .gitignore` and commit `chore: gitignore artifacts/, remove stale scaffold dirs and stray log` (only `.gitignore` is tracked; the rm/moves are working-tree only — note in the commit body what was cleaned).
- [ ] Step 5: run the full suite — confirm nothing depended on those paths (1751 passed, 1 pre-existing fail).

---

### Task 2 (B1): resolve runtime paths against the repo root, not CWD

**Closes:** B1 (CWD-dependent DB/artifacts path footgun) — also hardens the security-audit `contains()` CWD-dependence.

**Files:** `apps/api_gateway/app/core/settings.py` (the `database_url`/`artifacts_dir`/`stt_model_dir`-style defaults); anywhere those relative paths are consumed; test.

**Background:** `settings.py:57` `database_url = "sqlite+aiosqlite:///data/app.db"` and `artifacts_dir = "artifacts"` are relative → resolved against the process CWD, so a run from `apps/api_gateway/` created a stale second DB. Make them resolve against a stable repo root.

- [ ] Step 1: write a failing test — importing settings from a different CWD (or a unit test of a `_resolve_root()` helper) yields the SAME absolute path regardless of CWD. (Structural: assert the resolved db/artifacts path is anchored to the repo root, not `os.getcwd()`.)
- [ ] Step 2: run it red.
- [ ] Step 3: implement — introduce an explicit repo-root anchor. Prefer an `APP_ROOT` env var that defaults to the repo root computed from `__file__` (e.g. `Path(__file__).resolve().parents[N]`), and resolve `database_url`'s sqlite path + `artifacts_dir` + any model dir against it when they're relative. Keep absolute overrides (env-set absolute paths) working unchanged. Do NOT change the DEFAULT target location (must still be repo-root `data/app.db` / `artifacts/`), only make it CWD-independent. Confirm the model_service image's absolute `ARTIFACTS_DIR=/tmp/artifacts` / `DATABASE_URL=...` overrides still win.
- [ ] Step 4: green.
- [ ] Step 5: full suite. Because this touches path resolution used everywhere, watch for any test that relied on CWD-relative behavior — fix the test's assumption, not by reverting the anchor. Also re-confirm `artifact_store.contains()` (security) still behaves (its base_dir now anchors to repo root deterministically — a net improvement).
- [ ] Step 6: commit `fix(settings): resolve db/artifacts/model paths against the repo root, not CWD`.

---

### Task 3 (A2): hoist acyclic function-local imports to module top-level

**Closes:** A2 (~135 function-local `from app.…` imports, most not breaking cycles).

**Files:** the offenders — `main.py`, `core/auth_guard.py`, `api/routes/*.py`, `services/warmup.py`, `services/memory/extractor.py`, etc. Test: the suite (behavior-preserving) + optionally a lint-style test.

**Background:** Top deferred targets (`model_registry.store` 15×, `quota.gate` 10×, `providers.resolve` 10×, `usage.attribution` 6×) were audited ACYCLIC — cargo-culted lazy imports. A small subset (heavy ML modules in `model_registry/availability.py`, `services/models.py`) is legitimately deferred.

**Approach — incremental and verified, do NOT blindly hoist all 135:**
- [ ] Step 1: for each candidate target module, confirm hoisting doesn't create a cycle: move its function-local imports to the top of the consuming module, run `python`/pytest import; if `ImportError`/circular results, revert THAT one and leave it function-local with a comment `# function-local: breaks a cycle with X`. Start with the four confirmed-acyclic targets (store/gate/resolve/attribution).
- [ ] Step 2: explicitly LEAVE function-local: heavy-optional ML imports (document each with a one-line comment saying why), and any that fail step 1's cycle check.
- [ ] Step 3: run the full suite after each module's batch (import order changes can surface at import time, not just test time). Any collection error = a cycle you must leave deferred.
- [ ] Step 4: commit `refactor: hoist acyclic function-local imports to module scope` with the report noting which imports were LEFT function-local and why.

**Note:** A3 (moving auth_guard/identity_watch out of core) is NOT in scope — do not move files, only hoist imports. If hoisting a core→services import creates a cycle, leave it function-local (that cycle is A3's concern, deferred).

---

### Task 4 (A4/A7): centralize route request/response models into `schemas/`

**Closes:** A4 (35 inline BaseModel in routes; 3 duplicate `CloneRequest`), A7 (`schemas/tts.py` imports a service).

**Files:** `apps/api_gateway/app/schemas/<domain>.py` (new/expanded), `api/routes/*.py` (import from schemas instead of defining inline), `schemas/tts.py`.

- [ ] Step 1: inventory the 35 inline models (`grep -n "class .*BaseModel" api/routes/*.py`). Group by domain. Move each to `schemas/<domain>.py`, import it back into the route. Behavior-preserving — same fields, same validators. Dedup the three `CloneRequest` (`tts_profiles.py:52`, `mcp.py:49`, `profiles.py:131`) into one `schemas/common.py:CloneRequest` (confirm the three are actually identical before merging; if a field differs, keep separate and note it).
- [ ] Step 2 (A7): `schemas/tts.py:3` imports `app.services.artifacts`. Move whatever it needs (likely an artifacts-dir/containment helper) so the schema doesn't import a service — e.g. keep the containment check in the route/service layer (Task 2 area) and have the schema hold only the DTO. If the `ref_audio_path` field validator needs `artifact_store.contains`, that validator is security-relevant (from the authz rounds) — do NOT weaken it; relocate the check to keep the exact same rejection behavior (e.g. a route-level validator, mirroring how `tts_profiles.py` already does route-level containment). Re-run the `ref_audio_path` containment tests to prove identical behavior.
- [ ] Step 3: run the full suite; several tests import these models from their old inline location — update those imports (test-only churn, not a behavior change).
- [ ] Step 4: commit `refactor(schemas): centralize route request/response models`.

**CAUTION:** the `ref_audio_path` validator and any auth-related request model carry security behavior from the two authz rounds. Moving them must be pure relocation — verify the containment/traversal tests still pass unchanged.

---

### Task 5 (B4): mirror `tests/unit/` into per-package subdirectories

**Closes:** B4 (flat 213-file `tests/unit/`).

**Files:** move `tests/unit/test_*.py` into `tests/unit/<pkg>/` subdirs mirroring `app/services/`+`app/api`; ensure `__init__.py`/conftest discovery still works.

- [ ] Step 1: define the subdir mapping (by filename prefix → package): `auth/`, `conversation/`, `stt/`, `tts/`, `mcp/`, `livehost/`, `model_registry/`, `profiles/`, `db/`, `usage/`, `quota/`, `http/` (route/middleware/auth-guard tests), etc. `tests/unit/model_service/` already exists — follow that precedent.
- [ ] Step 2: `git mv` files in batches into the subdirs. After EACH batch, run `.venv/bin/pytest tests/unit -q` to confirm pytest still collects them (check `pyproject.toml`'s `testpaths`/`rootdir`/`pythonpath` config and whether `__init__.py` files are needed in the new subdirs — mirror whatever `tests/unit/model_service/` does). Do NOT change any test's contents — moves only.
- [ ] Step 3: confirm the FULL collected test count is unchanged (no test silently un-collected by the move). `.venv/bin/pytest tests/unit tests/integration -q` → same pass count as baseline + this task adds no tests.
- [ ] Step 4: commit `refactor(tests): mirror tests/unit into per-package subdirectories`.

**CAUTION:** the #1 risk is a move that makes pytest stop collecting a file (wrong `__init__.py`/conftest/rootdir). Guard by comparing the exact collected count before and after (`pytest --collect-only -q | tail -1`).

---

### Task 6 (A1): unify `livehost_stream` onto `ConversationSession`

**Closes:** A1 (the ~540-line livehost god-function duplicating the conversation engine). Biggest win, highest risk. Use TDD.

**Files:** `apps/api_gateway/app/api/routes/livehost.py`, `apps/api_gateway/app/services/conversation/session.py` (extend for a pluggable turn source), tests.

**Background:** `livehost_stream` (`livehost.py:149-698`) reimplements `session.py`'s turn loop inline (~15 closures). `conversation.py:331-518` is the template: build `SessionRuntimeConfig` + `ConversationSession`, wire `emit`/`emit_audio`. The difference: livehost turns are driven by social events (TikTok comments/gifts) + a poll loop, not raw voice frames.

**Approach:**
- [ ] Step 1: study both. Identify what `ConversationSession` needs to accept livehost's turn source: social-event-driven turns (`_run_social_turn`) vs voice turns, the `turn_lock`, the poll loop, and livehost-specific ownership/registry wiring (which the authz round added — MUST be preserved). Write the plan for the seam in your report BEFORE coding.
- [ ] Step 2: extend `ConversationSession` with a pluggable turn source / an injection point for social-event turns, keeping the voice path identical (all conversation/lugo tests must stay green). TDD: add tests for the new seam first.
- [ ] Step 3: rewrite `livehost_stream` to build a `ConversationSession` and wire livehost's emit/registry/ownership, deleting the duplicated closures. Preserve EXACTLY: the H5 ownership gate (`ws_session_owner_denied` + registry owner), quota metering, opus/audio wire-shape, idle/disconnect handling, and the livehost error wire-shape.
- [ ] Step 4: run ALL livehost + conversation + lugo tests, then the full suite. Behavior must be identical — livehost's existing tests (`test_livehost_*`, `test_livehost_authz.py`, `test_livehost_tts_profile.py`) are the spec; they must pass unchanged. If a livehost test needs to change, that's a behavior change — STOP and flag it.
- [ ] Step 5: commit `refactor(livehost): drive livehost_stream through ConversationSession`.

**CAUTION:** this is the riskiest task — a live WebSocket path carrying security behavior (H5), metering, and audio. It is behavior-preserving: the test suite (especially the authz + tts-profile livehost tests) is the contract. If unification proves to change observable behavior or balloon `session.py` incoherently, STOP and report — a partial extraction (shared helpers without full unification) may be the better outcome, and that's a judgment call to escalate, not force.

---

### Task 7: full verification

- [ ] Full suite `.venv/bin/pytest tests/unit tests/integration -q` → 1751+ passed (Tasks add tests), only the pre-existing `test_stt_ws.py` failure.
- [ ] Re-confirm no behavior/security regression: run the authz test set (`test_auth_guard*`, `test_profile_idor`, `test_mcp_*`, `test_livehost_authz`, `test_pairing_hardening`, `test_ref_audio_path_containment`, `test_upload_size_limit_middleware`) — all must pass, proving the refactors preserved the two authz rounds' fixes.
- [ ] Record results to `.superpowers/sdd/2026-07-29-structure-refactor/final-verification.md`.

---

## Self-Review
- Hygiene B2/B3/B5/B6 → Task 1; B1 → Task 2; A2 → Task 3; A4/A7 → Task 4; B4 → Task 5; A1 → Task 6; verify → Task 7.
- OUT of scope (not selected): A3 (auth_guard out of core), A5 (migration runner), A6 (naming convention), A8, and LOW B7/B8/B9. Left for a later pass.
- Every task is behavior-preserving; the suite + the authz test set are the guards.
