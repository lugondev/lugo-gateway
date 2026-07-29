# Codebase structure audit — 2026-07-29

Two read-only agents audited (1) Python code architecture and (2) repo files/folders
hygiene. Verdict: **the architecture is fundamentally sound — real
routes→services→db layering, a clean provider abstraction reused by both apps, a
shared `SqliteBackedStore` base.** What's eroding it is convention drift and a few
concrete layout footguns, not rot. Nothing here is a rewrite.

## Cross-cutting insight

Two findings trace to root causes we already hit in the security work:
- **CWD-relative path defaults** (`settings.database_url`/`artifacts_dir` resolve
  against the process CWD) produced the stale second DB below — and are the SAME
  root cause as the security audit's `contains()` CWD-dependence. One fix
  (resolve paths against an explicit repo root / `APP_ROOT`) closes both.
- **The livehost turn-loop duplication** (A1 below) is WHY the security H5 livehost
  IDOR had to be fixed separately from conversation/lugo — the copy didn't inherit
  the shared gate. Structural debt directly caused duplicated security fixes.

---

## Code architecture (agent A, opus)

### HIGH
- **A1 — `api/routes/livehost.py:149-698`: a ~540-line `livehost_stream` god-function that reimplements the conversation engine inline.** ~15 nested closures (`_stream_to_tts`, `_synth`, `_run_voice_turn`, `_record_llm_usage`, turn-lock state…) near-verbatim duplicating `services/conversation/session.py`; it uses `ConversationSession` NOWHERE (grep-confirmed). Sibling `conversation.py:331-518` does it right (~180 lines wiring `SessionRuntimeConfig`+`ConversationSession`). Every turn-loop fix must be applied twice and the copies have drifted. **Fix:** extend `ConversationSession` with a pluggable turn source (voice frames vs social-event turns), delete the inline copy. *Biggest single win.*
- **A2 — ~135 function-local `from app.…` imports hide the dependency graph; most are NOT breaking real cycles.** Top deferred targets (`model_registry.store` 15×, `quota.gate` 10×, `providers.resolve` 10×, `usage.attribution` 6×) were traced acyclic → cargo-culted lazy-loading, not cycle workarounds. A small subset (heavy ML modules in `model_registry/availability.py`, `services/models.py`) is legitimate. **Fix:** hoist the acyclic ones to module top-level; keep function-local only for confirmed cycles/heavy-optional deps, with a comment saying which.
- **A3 — `core/` is not a leaf layer.** `core/auth_guard.py:12` (+ function-local at :194,:351-352,:446,:451) and `core/identity_watch.py:72-73` import `services/`. The other 13 core files are correctly leaf. `core` is imported by everything, so depending back on `services` is the import-order fragility that spawned the function-local imports. **Fix:** move `auth_guard`/`identity_watch` into `services/auth/` (they're auth middleware, not primitives), or invert via an interface in `core`.

### MEDIUM
- **A4 — request/response models scattered inline in routes, not `schemas/`.** 35 `class …(BaseModel)` inside `api/routes/*.py`; `schemas/` holds only 4 tiny files (90% unused). Three duplicate `CloneRequest` (`tts_profiles.py:52`, `mcp.py:49`, `profiles.py:131`). `model_service` already imports `app.schemas.tts.TTSRequest` — proof the pattern is wanted. **Fix:** move route models into `schemas/<domain>.py`.
- **A5 — `main.py:133-161` lifespan carries a 10-step migration/seeding orchestration** with ordering-dependency comments — business logic buried in the ASGI entry point. Rest of `main.py` (319 lines) is legit wiring. **Fix:** extract `services/migrations/runner.py` / `run_startup_migrations()`; lifespan calls it in one line.
- **A6 — persistence pattern bimodal + service-package shape inconsistent.** Config-blob stores subclass `SqliteBackedStore`; relational stores use raw SQLModel — defensible, but naming isn't (`store.py` vs `server_store.py` vs `profile_store.py` vs `auth/users.py`). Only `stt`/`tts`/`recommend` expose `service.py`; others expose a module-level singleton. No predictable "where's the entry point / persistence" per domain. **Fix:** adopt + document a convention, rename outliers.

### LOW
- **A7 — `schemas/tts.py:3` imports `app.services.artifacts`** (schema depending on a service; schemas should be leaf DTOs). **Fix:** move the helper out or invert.
- **A8 — fat-vs-thin route inconsistency** (`model_registry.py:485-489` mixes CRUD + availability probe + price bulk-edit + cache-reset). **Fix:** push logic into services.
- **A9 — no dead code found.** All flagged candidates (`llm_models.py`, `system_config.py` 22×, `models.py`, `whisper_models.py`, `install_manager.py`, `vad.py`) are live.

### Well-structured (praise)
Provider abstraction (`stt/base.py`/`tts/base.py`) reused by model_service via interface-only imports — coupling done right. `SqliteBackedStore` proper generic base. `core/` 13/15 leaf. `conversation.py /stream` is the template livehost should follow. `main.py` middleware ordering correct + exhaustively documented. `session.py` (992 lines) large but cohesive (a real state machine).

**Suggested refactor order:** A1 (livehost unify) → A2 (hoist imports) → A3 (auth_guard out of core) → A4+A7 (schemas pass) → A5 (migration runner) → A6 (naming, last — highest churn).

---

## Repo files/folders hygiene (agent B, sonnet)

### HIGH
- **B1 — `apps/api_gateway/data/app.db` (32K, stale Jul 23) is a CWD-dependent footgun.** `settings.py:57` `database_url = "sqlite+aiosqlite:///data/app.db"` (relative to CWD). `make dev` runs from repo root → writes root `data/` (live, 520K, Jul 29). But any run with CWD=`apps/api_gateway/` silently reads/creates this stale second DB — data loss/confusion; it already happened once. **Fix:** delete `apps/api_gateway/data/`; resolve `database_url`/`artifacts_dir`/`stt_model_dir` against repo root or `APP_ROOT`, not CWD (also closes the security-audit `contains()` CWD issue).
- **B2 — root `artifacts/` (2.3M, 155 wav/mp3) is NOT gitignored** (nor `apps/api_gateway/artifacts/`). `.gitignore` has `data/`+`models/` but no `artifacts/`. Untracked today by luck, not design — one `git add -A` commits 2.3M of generated audio. **Fix:** add bare `artifacts/` to `.gitignore`.

### MEDIUM
- **B3 — empty scaffold dirs** `apps/api_gateway/{data,artifacts,models}/` — byproduct of the CWD path defaults; delete once B1's path fix lands.
- **B4 — `tests/unit/` is a flat 213-file dir** while source is 14 subpackages; filenames already encode grouping, and `tests/unit/model_service/` is already nested → inconsistent even internally. **Fix:** mirror `app/services/<pkg>/` under `tests/unit/<pkg>/` (incrementally).
- **B5 — `data/` holds 3 unbounded ad-hoc `.bak-*` files** (520K each, no rotation). **Fix:** `data/backups/` + prune policy.
- **B6 — stray `uvicorn-8000.log` at root** from a manual run; Makefile convention is `.run/gateway.log`. **Fix:** delete; redirect manual runs to `.run/`.

### LOW
- **B7 — no `LICENSE`/`CONTRIBUTING.md`** (fine for private, worth a conscious call).
- **B8 — `apps/model_service/vendor/qwen3-asr.cpp` is 3.1GB** (correctly ignored) — dominates `apps/` disk.
- **B9 — `scripts/rpi_voice_client.py`** is a device-client script misplaced among repo-maintenance scripts (minor).
- `docs/superpowers/` (127 files, 3.4M) large but self-contained — fine. `scripts/` (9 files) fine. `infra/` well-organized.

### Clean
`.gitignore` otherwise thorough; submodules coherently grouped (devices at root, `servers/*` grouped); `apps/` coherent two-app layout; no secrets/generated content tracked.

---

## Quick-win vs refactor split (for triage)
- **Pure hygiene, cheap + safe:** B2 (gitignore artifacts), B3/B6 (delete scaffold+log), B5 (backups dir). Minutes, no code risk.
- **Small code fix, high leverage:** B1 (repo-root path resolution) — also hardens the security `contains()` behavior.
- **Mechanical refactors:** A2 (hoist imports), A4+A7 (schemas centralization), B4 (test dir mirroring).
- **Bigger refactors (test-heavy, do via TDD/subagent flow):** A1 (livehost unify — biggest win, riskiest), A3 (auth_guard out of core), A5 (migration runner), A6 (naming convention).
