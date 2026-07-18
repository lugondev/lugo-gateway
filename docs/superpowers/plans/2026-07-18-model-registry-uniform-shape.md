# Model Registry Uniform Shape Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the Model Registry from mirroring a code-owned catalogue: delete `seed_known_models()`, correct the docstrings that describe its now-gone governance rows, and clean the 27 catalogue rows out of the live database.

**Architecture:** The registry holds configuration + optional restriction, not a per-variant catalogue. The catalogue stays in `whisper_models.py`. Sentinel `model_id=""` rows (real config) come from migrations and are untouched. `gate.py`'s "no matching entry → unrestricted" already makes seeded permissive rows meaningless, so removing them changes no behavior.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy (async + sync engines over one SQLite file), pytest.

**Spec:** `docs/superpowers/specs/2026-07-17-model-registry-uniform-shape-design.md`

## Global Constraints

- **Use `.venv/bin/python` for every python/pytest command.** The default `python` is pyenv 3.14 and lacks this project's ML deps; `.venv` is Python 3.12. Do NOT create a venv or a `.python-version` file.
- Run pytest backgrounded — `(cmd) & wait $!` — the repo's `tests/concurrency_guard.py` can false-positive in the foreground.
- Baseline: `.venv/bin/python -m pytest -q` → **1 failed, 1026 passed**. The single failure is `tests/unit/test_provider_single_flight_load.py::test_vieneu_provider_builds_model_once_under_race` (vieneu not installed) — pre-existing, leave it. Any OTHER failure is yours.
- `seed_known_models` is **deleted outright**, not kept as a no-op stub.
- The sentinel-creating migrations and their `find("stt", engine, "")` guards are **unchanged**.
- `routes/profiles.py`'s `registry.validate()`, `whisper_models.py`'s `WHISPER_SIZES`/`_VALID_SIZES`, and `gate.py` are **unchanged**.
- Do NOT weaken any test to make a change pass. Tests that guarded the shadow-row bug are rewritten to keep pinning the same property (resolvers target the `model_id=""` sentinel, never an arbitrary enabled row), not deleted.
- New default after PhoWhisper removal: `whisper_local` default_model is `large-v3-turbo`; `whisper_mlx` model_path is `models/stt/whisper-large-v3-turbo-mlx`. Do not reintroduce phowhisper.

## File Structure

| File | Change |
|---|---|
| `apps/api_gateway/app/services/model_registry/seed.py` | delete `seed_known_models()`; fix 2 stale docstrings in migrations |
| `apps/api_gateway/app/main.py` | remove `seed_known_models` import + call from lifespan |
| `apps/api_gateway/app/services/model_registry/resolve.py` | fix 4 stale docstring references |
| `tests/unit/test_model_registry_seed.py` | replace seed-existence tests with "seed is gone" |
| `tests/unit/test_model_registry_seed_migration.py` | rewrite 2 shadow-guard tests to build the shadow row by hand |
| `tests/unit/test_model_registry_resolve.py` | update 3 docstrings (tests already build rows by hand) |
| (live DB, not committed) | delete 27 catalogue rows via a one-off script |

---

### Task 1: Delete `seed_known_models` and unwire it

**Files:**
- Modify: `apps/api_gateway/app/services/model_registry/seed.py:15-22` (delete the function)
- Modify: `apps/api_gateway/app/main.py:130-139` (remove import + call)
- Modify: `tests/unit/test_model_registry_seed.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `seed_known_models` no longer exists in `app.services.model_registry.seed`. A fresh database, after the lifespan runs, contains only migration-created rows.

- [ ] **Step 1: Rewrite the seed test to pin the new reality**

Replace the body of `tests/unit/test_model_registry_seed.py` entirely with:

```python
import pytest

from app.services.model_registry import seed as seed_module
from app.services.model_registry.store import ModelRegistryStore


def test_seed_known_models_is_gone():
    """The registry no longer mirrors the code-owned catalogue. gate.py treats a
    missing entry as unrestricted, so seeding a permissive row per known model
    restricted nothing -- it only manufactured shadow rows. The catalogue lives
    in whisper_models.py; the registry holds config + restriction."""
    assert not hasattr(seed_module, "seed_known_models")


@pytest.mark.asyncio
async def test_fresh_store_has_no_catalogue_rows():
    """A store nobody has seeded is empty -- no per-variant STT rows, no
    model_id==engine TTS placeholder rows. (Migrations add the model_id=""
    sentinels; those are exercised in the migration tests, not here.)"""
    store = ModelRegistryStore()
    assert await store.list_all() == []
```

- [ ] **Step 2: Run it, watch it fail**

Run: `(.venv/bin/python -m pytest tests/unit/test_model_registry_seed.py -q) & wait $!`
Expected: FAIL — `test_seed_known_models_is_gone` fails because `seed_known_models` still exists (`hasattr` returns True), and the old import line `from app.services.model_registry.seed import seed_known_models` at the top is gone so any leftover reference errors. (`test_fresh_store_has_no_catalogue_rows` already passes — an unseeded store is empty.)

- [ ] **Step 3: Delete the function**

In `apps/api_gateway/app/services/model_registry/seed.py`, delete `seed_known_models` (lines 15-22, the whole `async def seed_known_models(): ...` block). Then remove any now-unused import: check whether `tts_service` (imported at the top) is still referenced elsewhere in the file — grep `grep -n tts_service apps/api_gateway/app/services/model_registry/seed.py`. If `seed_known_models` was its only user, delete the `from app.services.tts.service import tts_service` import too. Do the same check for `STT_MODEL_REGISTRIES`.

Also update the module docstring (lines 1-5): it currently describes the seed's purpose ("registers every model the STT registries and installed TTS engines already know about"). Replace it with a description of what the module does now — migrations that back-fill config-carrying `model_id=""` sentinel rows from legacy SystemConfig.

- [ ] **Step 4: Remove the call from lifespan**

In `apps/api_gateway/app/main.py`, in the import block (around lines 130-137) remove the `seed_known_models,` line, and remove the `await seed_known_models()` call (around line 139). Leave the five `migrate_*` imports and calls exactly as they are.

- [ ] **Step 5: Run the seed test + a broad import check**

Run: `(.venv/bin/python -m pytest tests/unit/test_model_registry_seed.py -q) & wait $!`
Expected: 2 passed.

Run: `(.venv/bin/python -c "import app.main" ) & wait $!`
Expected: no ImportError (proves main.py has no dangling `seed_known_models` reference).

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/services/model_registry/seed.py apps/api_gateway/app/main.py tests/unit/test_model_registry_seed.py
git commit -m "refactor(model-registry): remove seed_known_models

The registry mirrored whisper_models.py's catalogue by seeding a permissive
row per known model (and a model_id==engine placeholder per TTS engine). Per
gate.py a missing entry is already unrestricted, so those rows restricted
nothing -- they only manufactured the tts/openai_tts shadow row and the
find_enabled_sync hazard. Registry now holds config + restriction only."
```

---

### Task 2: Fix the stale docstrings the seed left behind

**Files:**
- Modify: `apps/api_gateway/app/services/model_registry/resolve.py` (lines ~100, 116-118, 132-133)
- Modify: `apps/api_gateway/app/services/model_registry/seed.py` (lines ~88-89, 167-168)
- Modify: `tests/unit/test_model_registry_resolve.py` (docstrings at lines ~28, 78, 110)

**Interfaces:**
- Consumes: Task 1 (seed_known_models gone).
- Produces: no behavior change — comments/docstrings only. The `find_sync(kind, engine, "")` calls are unchanged.

Why: several docstrings justify targeting the `model_id=""` sentinel by warning that `seed_known_models()` creates enabled governance rows that `find_enabled_sync` would match. With the seed gone, the justification is now "an admin *could* create such a row by hand" — the sentinel targeting is still correct, but the reason text is wrong.

- [ ] **Step 1: Update `resolve.py` docstrings**

In `apps/api_gateway/app/services/model_registry/resolve.py`, in the docstrings of `resolve_stt_engine_config`, `resolve_stt_local_device`, and `resolve_omnivoice_config`, replace every phrase that says the governance/per-model-size rows are something "`seed_known_models()` creates" with the fact that an admin *may* create additional enabled rows under the same `(kind, engine)` by hand (a per-model restriction row), which is exactly why these resolvers must target the reserved `model_id=""` sentinel rather than `find_enabled_sync`. Keep the technical claim (must target the sentinel); only the source of the colliding row changes from "the seed" to "an admin".

Concretely, the three spots:
- `resolve_stt_local_device` (~line 116-118): "...distinct from the per-model-size governance rows seed_known_models() creates under the same (kind, engine) pair -- using find_enabled_sync here instead would silently match one of those governance rows" → "...distinct from any per-model restriction row an admin may add under the same (kind, engine) pair -- using find_enabled_sync here would silently match such a row instead of the config sentinel".
- `resolve_stt_engine_config` (~line 100): the cross-reference "see resolve_stt_local_device's docstring for why the per-model-size governance rows must not match" → "...for why an arbitrary per-model row must not match".
- `resolve_omnivoice_config` (~line 132-133): "seed_known_models() creates a separate tts/omnivoice/omnivoice governance row that would otherwise be ambiguous" → "an admin may add a tts/omnivoice restriction row that would otherwise be ambiguous with this config sentinel".

- [ ] **Step 2: Update `seed.py` migration docstrings**

In `apps/api_gateway/app/services/model_registry/seed.py`, two migration docstrings reference the seed:
- `migrate_stt_local_models_to_registry` (~line 88-89): "model_id="" -- distinct from the per-size governance rows seed_known_models() already creates" → "model_id="" -- the engine-level config row, distinct from any per-model row an admin may add".
- `migrate_omnivoice_to_registry` (~line 167-168): "sentinel (distinct from the tts/omnivoice/omnivoice governance row seed_known_models() already creates)" → "sentinel (the config row; distinct from any tts/omnivoice restriction row an admin may add)".

- [ ] **Step 3: Update the three test docstrings**

In `tests/unit/test_model_registry_resolve.py`, the tests at ~lines 28, 78, 110 already build the colliding row by hand with `model_registry_store.create(...)` — only their docstrings claim `seed_known_models()` is the source. Change each docstring's "seed_known_models() creates ... ENABLED row" phrasing to "an admin may create an ENABLED row under the same (kind, engine) with a non-empty model_id". The test bodies and assertions are unchanged — they still prove the resolver returns the `model_id=""` sentinel's config, not the colliding row.

- [ ] **Step 4: Run the affected suites**

Run: `(.venv/bin/python -m pytest tests/unit/test_model_registry_resolve.py -q) & wait $!`
Expected: all pass (docstring-only changes; behavior identical).

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/model_registry/resolve.py apps/api_gateway/app/services/model_registry/seed.py tests/unit/test_model_registry_resolve.py
git commit -m "docs(model-registry): correct docstrings that named the deleted seed

The sentinel-targeting resolvers must still avoid find_enabled_sync, but the
colliding enabled row now comes from an admin adding a per-model restriction
row by hand, not from seed_known_models()."
```

---

### Task 3: Rewrite the two shadow-guard migration tests

**Files:**
- Modify: `tests/unit/test_model_registry_seed_migration.py` (the tests at ~line 96 and ~line 218)

**Interfaces:**
- Consumes: Task 1 (seed_known_models gone).
- Produces: two regression tests that still pin "the migration's sentinel is not shadowed by a colliding enabled row", now building that row by hand instead of via the seed.

Why: `test_migrate_stt_local_device_not_shadowed_by_seed_known_models_row` and `test_migrate_omnivoice_not_shadowed_by_seed_known_models_row` currently call `seed_known_models()` to manufacture the shadow, then run the migration and assert the sentinel still resolves correctly. The property is still worth pinning — an admin can create a colliding enabled row — but the test can no longer call the deleted seed.

- [ ] **Step 1: Read the two tests to see exactly what they build and assert**

Run: `sed -n '90,140p;212,260p' tests/unit/test_model_registry_seed_migration.py`

Note for each: what row `seed_known_models()` was creating that acted as the shadow (for stt_local_device it is an enabled `(stt, whisper_local, <some model_id>)` row with `config={}`; for omnivoice an enabled `(tts, omnivoice, "omnivoice")` row), which migration runs, and what the final assertion checks (that the migration's `model_id=""` sentinel carries the right config / that the resolver returns it).

- [ ] **Step 2: Replace the `seed_known_models()` call in each with a hand-built row**

In each test, delete the `await seed_known_models()` line (and its import) and replace it with a direct `model_registry_store.create(...)` that builds the same colliding row the seed used to build:

- stt_local_device test: `await model_registry_store.create("stt", "whisper_local", "large-v3-turbo", "Whisper Large v3 Turbo", config={})` (any non-empty model_id under the same engine, enabled, empty config — reproduces the shadow).
- omnivoice test: `await model_registry_store.create("tts", "omnivoice", "omnivoice", "OmniVoice", config={})` (the exact placeholder the seed used).

Rename each test to drop `seed_known_models` from the name (e.g. `test_migrate_stt_local_device_not_shadowed_by_a_per_model_row`, `test_migrate_omnivoice_not_shadowed_by_a_placeholder_row`) and update the docstring from "Regression guard for the exact bug found in final review: seed_known_models() creates..." to "Regression guard: a colliding enabled row under the same (kind, engine) -- e.g. one an admin adds -- must not shadow the migration's model_id='' config sentinel." Keep the migration call and the final assertions byte-for-byte.

- [ ] **Step 3: Run the migration suite**

Run: `(.venv/bin/python -m pytest tests/unit/test_model_registry_seed_migration.py -q) & wait $!`
Expected: all pass. If a test now fails, the migration genuinely depends on the seed having run first — STOP and report it, do not weaken the assertion.

- [ ] **Step 4: Mutation-check one of them**

Confirm the rewritten test still bites: in `resolve.py` (or the migration), the sentinel lookup uses `find("stt", engine, "")`. Temporarily change the stt_local_device migration's guard from `find("stt", "whisper_local", "")` to `find_enabled("stt", "whisper_local")` (the exact bug the test guards), run the test, confirm it FAILS, then revert. Verify `git diff` shows the file restored before continuing.

Run: `(.venv/bin/python -m pytest tests/unit/test_model_registry_seed_migration.py -k shadow -q) & wait $!`
Expected after mutation: FAIL. After revert: PASS. `git diff apps/api_gateway/app/services/model_registry/seed.py` must be empty.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_model_registry_seed_migration.py
git commit -m "test(model-registry): shadow-guard tests build the colliding row by hand

The migrations must still target the model_id='' sentinel, not find_enabled.
The tests used to manufacture the colliding row via seed_known_models(); with
the seed gone they build it directly, pinning the same property."
```

---

### Task 4: Full suite + clean the live database

**Files:**
- No source files. A one-off cleanup script against the live DB (not committed).

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces: the live registry DB has the 27 catalogue rows removed and the 15 config rows kept.

- [ ] **Step 1: Full suite green (except the known baseline failure)**

Run: `(.venv/bin/python -m pytest -q) & wait $!`
Expected: `1 failed, N passed` where the single failure is `test_vieneu_provider_builds_model_once_under_race`. Any other failure is a regression from Tasks 1-3 — fix before proceeding.

This run is also what confirms the spec's non-negotiable: `check_model_allowed` still blocks disabled and testing-stage rows. `gate.py` is untouched by this plan, so its existing tests (grep `check_model_allowed` under `tests/`) carry that guarantee — confirm they are in the passing set, not skipped.

- [ ] **Step 2: Back up the live database**

Run: `cp /Users/lugon/code/speech-text-transformer/data/app.db /tmp/app.db.bak-registry-cleanup`
Expected: file copied. This is real data; the backup is the undo.

- [ ] **Step 3: Dry-run the classification (read-only)**

```bash
.venv/bin/python - <<'PY'
import sqlite3, json
DB = "/Users/lugon/code/speech-text-transformer/data/app.db"
rows = sqlite3.connect(DB).execute(
    "SELECT id,kind,engine,model_id,enabled,base_url,api_key,config,stage FROM model_registry_entries").fetchall()
keep, drop = [], []
for r in rows:
    _id,k,e,m,en,url,key,cfg,stage = r
    c = json.loads(cfg or "{}")
    # keep anything carrying real config, a url, a key, or that is disabled / non-stable
    real = bool(url or key or c) or (stage != "stable") or (not en)
    (keep if real else drop).append((k,e,m))
print(f"KEEP {len(keep)}:")
for x in sorted(keep): print("   ", x)
print(f"DROP {len(drop)}:")
for x in sorted(drop): print("   ", x)
PY
```
Expected: KEEP 15, DROP 27. The DROP list must be exactly the whisper/whisper_local per-size rows, `qwen3_asr/0.6b`, `qwen3_asr/1.7b`, the 7 `tts/<engine>/<engine>` placeholders, and `tts/openai_tts/openai_tts`. **Read the DROP list before proceeding — if anything with a base_url, api_key, or non-empty config appears in it, STOP.**

- [ ] **Step 4: Delete the catalogue rows**

```bash
.venv/bin/python - <<'PY'
import sqlite3, json
DB = "/Users/lugon/code/speech-text-transformer/data/app.db"
con = sqlite3.connect(DB)
rows = con.execute(
    "SELECT id,enabled,base_url,api_key,config,stage FROM model_registry_entries").fetchall()
dropped = 0
for _id,en,url,key,cfg,stage in rows:
    c = json.loads(cfg or "{}")
    real = bool(url or key or c) or (stage != "stable") or (not en)
    if not real:
        con.execute("DELETE FROM model_registry_entries WHERE id=?", (_id,))
        dropped += 1
con.commit()
remaining = con.execute("SELECT count(*) FROM model_registry_entries").fetchone()[0]
con.close()
print(f"dropped {dropped}, remaining {remaining}")
PY
```
Expected: `dropped 27, remaining 15`.

- [ ] **Step 5: Verify the registry resolves correctly after cleanup**

```bash
cd /Users/lugon/code/stt-model-service && PYTHONPATH=apps/api_gateway:apps DATABASE_URL="sqlite+aiosqlite:////Users/lugon/code/speech-text-transformer/data/app.db" ARTIFACTS_DIR=/tmp/ms-artifacts .venv/bin/python - <<'PY'
import asyncio
from app.services.model_registry.store import model_registry_store as s
async def go():
    for kind, eng in [("stt","openai_stt"), ("tts","openai_tts")]:
        e = await s.find_enabled(kind=kind, engine=eng)
        print(f"find_enabled({kind},{eng}) -> {e['model_id']!r} @ {e['base_url']}" if e else f"{kind}/{eng}: NONE")
    e = await s.find_enabled(kind="llm")
    print(f"find_enabled(llm) -> {e['engine']!r}/{e['model_id']!r}")
    # sentinels survived
    for eng in ("whisper_local","qwen3_asr","vosk","whisper_mlx"):
        r = await s.find("stt", eng, "")
        print(f"sentinel stt/{eng}/'' -> {'present' if r else 'MISSING'}")
asyncio.run(go())
PY
```
Expected: `openai_stt -> 'Qwen/Qwen3-ASR-0.6B'`, `openai_tts -> 'vieneu'`, `llm -> 'openrouter'/...`, all four sentinels `present`. If any sentinel is MISSING, the cleanup deleted a config row — restore from the backup and investigate.

- [ ] **Step 6: Confirm the shadow can no longer be resolved**

The `tts/openai_tts/openai_tts` shadow row is now deleted, and Task 1 stopped it being recreated. Confirm `find_enabled(tts, openai_tts)` returns the real `vieneu` entry (base_url set), which Step 5 already prints. No separate command needed — just confirm the base_url in Step 5's output is `http://127.0.0.1:8101/v1`, not empty.

---

## Verification

After Task 4, the whole change is verified by:
1. Full suite: 1 failed (baseline vieneu) / rest passed.
2. Live DB: 15 rows, all four config sentinels present, service engines resolve to real base_urls.
3. `seed_known_models` is gone (`.venv/bin/python -c "from app.services.model_registry import seed; assert not hasattr(seed, 'seed_known_models')"`).
4. Restarting the gateway does not recreate any catalogue row (the seed call is gone from lifespan) — spot-check by re-running Step 5 after a fresh `import app.main` lifespan, or trust Task 1's test + the removed call.
