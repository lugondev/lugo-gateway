# Model Registry Single Source of Truth — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Model Registry the single catalog every profile/service reads to choose an STT/TTS/LLM model, with local-model installs auto-syncing into the registry.

**Architecture:** Two clean concepts — "Models" (artifact lifecycle: download/install/delete) and "Model Registry" (source of truth for selection). Model dropdowns read a new unified `GET /v1/model_registry/options?kind=` endpoint (enabled + stage-filtered). Installing a local model auto-creates/enables its registry entry; deleting disables it. Gate becomes catalog-mode (must have an enabled entry). The confusingly-named `stt/model_registry.py` is renamed to `stt/model_catalog.py`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy (async), pytest/pytest-asyncio; vanilla ES-module JS (playground) + React/TypeScript/Vitest (lugo-web-client submodule).

## Global Constraints

- Dev venv is **Python 3.12** (`.venv` at repo root). Run backend tests from `apps/api_gateway`.
- Registry entry shape: `(kind, engine, model_id, label, enabled, stage, api_key, base_url, config)`. `kind ∈ {"stt","tts","llm"}`. `stage ∈ {"stable","testing"}`.
- Registry has **no DELETE** — "delete a model" means `enabled=false`, row retained (preserves api_key/config).
- `model_id=""` is a reserved **engine-config sentinel row** (device/compute config), NOT a selectable model — options endpoint and auto-sync must skip `model_id==""`.
- Never overwrite admin-edited config/api_key/base_url/stage on an existing entry (idempotent seed/auto-sync).
- Commit as `lugondev <lugondev@gmail.com>`. End commit messages with the Co-Authored-By trailer.
- Test scope: run only the changed repo's tests during dev; full suite is the pre-commit gate.

---

### Task 1: Rename `stt/model_registry.py` → `stt/model_catalog.py`

Mechanical, behavior-preserving. Kills the name collision with `services/model_registry/`.

**Files:**
- Rename: `apps/api_gateway/app/services/stt/model_registry.py` → `apps/api_gateway/app/services/stt/model_catalog.py`
- Rename symbol: `STT_MODEL_REGISTRIES` → `STT_MODEL_CATALOGS` (inside that file)
- Modify: `apps/api_gateway/app/api/routes/stt.py:104,106,128` (imports of `STT_MODEL_REGISTRIES` / `apply_stt_model`)
- Modify: `apps/api_gateway/app/api/routes/profiles.py:12,33` (import + usage of `STT_MODEL_REGISTRIES`)
- Modify: `apps/api_gateway/app/services/stt/model_registry.py:85` comment reference in `routes/stt.py` docstring (`see app.services.stt.model_registry` → `model_catalog`)

**Interfaces:**
- Produces: module `app.services.stt.model_catalog` exporting `STT_MODEL_CATALOGS: dict[str, object]`, `apply_stt_model(engine, model)`, `resolve_default_stt_model(engine)`, `qwen3_asr_model_registry`, `Qwen3AsrModelRegistry`.

- [ ] **Step 1: Find every reference to the old module/symbol**

Run: `cd apps/api_gateway && grep -rn "stt.model_registry\|stt/model_registry\|STT_MODEL_REGISTRIES" app tests`
Expected: hits in `routes/stt.py`, `routes/profiles.py`, possibly `services/stt/profile.py` and tests. Note every file.

- [ ] **Step 2: Rename the file (git mv) and the symbol**

```bash
cd apps/api_gateway
git mv app/services/stt/model_registry.py app/services/stt/model_catalog.py
```

In `app/services/stt/model_catalog.py`, rename the dict:
```python
STT_MODEL_CATALOGS: dict[str, object] = {
    "whisper": whisper_manager,
    "whisper_local": whisper_manager,
    "qwen3_asr": qwen3_asr_model_registry,
}
```
And update `apply_stt_model` body to reference `STT_MODEL_CATALOGS.get(engine)`.

- [ ] **Step 3: Update all importers**

`routes/stt.py`: change `from app.services.stt.model_registry import STT_MODEL_REGISTRIES` → `from app.services.stt.model_catalog import STT_MODEL_CATALOGS`, and `from app.services.stt.model_registry import apply_stt_model` → `from app.services.stt.model_catalog import apply_stt_model`. Update the two usages of `STT_MODEL_REGISTRIES` in `list_stt_models`.

`routes/profiles.py`: change `from app.services.stt.model_registry import STT_MODEL_REGISTRIES` → `from app.services.stt.model_catalog import STT_MODEL_CATALOGS`, and update line 33 `registry = STT_MODEL_CATALOGS.get(engine)`.

Update any other files found in Step 1 (e.g. `services/stt/profile.py`) the same way.

- [ ] **Step 4: Run the full api_gateway suite to prove nothing broke**

Run: `cd apps/api_gateway && ../../.venv/bin/python -m pytest -q`
Expected: PASS (same count as before the rename; no ImportError).

- [ ] **Step 5: Commit**

```bash
cd /Users/lugon/code/speech-text-transformer
git add apps/api_gateway/app/services/stt/model_catalog.py apps/api_gateway/app/api/routes/stt.py apps/api_gateway/app/api/routes/profiles.py
git add -A apps/api_gateway
git commit -m "refactor(stt): rename model_registry.py to model_catalog.py to end name collision

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Add `list_options(kind, can_use_testing)` helper on the store

The filtering logic for the options endpoint, unit-tested in isolation before wiring the route.

**Files:**
- Modify: `apps/api_gateway/app/services/model_registry/store.py` (add method to `ModelRegistryStore`)
- Test: `apps/api_gateway/tests/services/model_registry/test_store_options.py`

**Interfaces:**
- Produces: `async ModelRegistryStore.list_options(kind: str, can_use_testing: bool) -> list[dict]` returning `[{"engine","model_id","label"}]` for entries where `kind` matches, `enabled` is true, `model_id != ""` (skip config sentinel rows), and (`stage == "stable"` or `can_use_testing`). Sorted by `(engine, model_id)`.

- [ ] **Step 1: Write the failing test**

```python
# apps/api_gateway/tests/services/model_registry/test_store_options.py
import pytest
from app.services.model_registry.store import model_registry_store


@pytest.mark.asyncio
async def test_list_options_filters_enabled_stable_and_skips_sentinel(tmp_db):
    await model_registry_store.create("stt", "whisper", "tiny", "Tiny", enabled=True)
    await model_registry_store.create("stt", "whisper", "large-v3", "Large", enabled=True)
    await model_registry_store.create("stt", "whisper", "", "Whisper config", enabled=True)  # sentinel
    await model_registry_store.create("stt", "vosk", "vn", "Vosk VN", enabled=False)  # disabled
    await model_registry_store.create("stt", "qwen3_asr", "1.7b", "Q Testing", enabled=True, stage="testing")

    stable = await model_registry_store.list_options("stt", can_use_testing=False)
    assert stable == [
        {"engine": "whisper", "model_id": "large-v3", "label": "Large"},
        {"engine": "whisper", "model_id": "tiny", "label": "Tiny"},
    ]

    with_testing = await model_registry_store.list_options("stt", can_use_testing=True)
    assert {"engine": "qwen3_asr", "model_id": "1.7b", "label": "Q Testing"} in with_testing
    assert len(with_testing) == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/api_gateway && ../../.venv/bin/python -m pytest tests/services/model_registry/test_store_options.py -v`
Expected: FAIL with `AttributeError: 'ModelRegistryStore' object has no attribute 'list_options'`

- [ ] **Step 3: Implement the method**

Add to `ModelRegistryStore` in `store.py` (after `list_all`):
```python
async def list_options(self, kind: str, can_use_testing: bool) -> list[dict]:
    """Selectable entries for a dropdown: enabled, non-sentinel (model_id != ""),
    and stage-visible to the caller. Config sentinel rows (model_id == "") are
    engine config, not selectable models, so they're excluded."""
    await self._ensure_loaded()
    opts = [
        {"engine": e["engine"], "model_id": e["model_id"], "label": e["label"]}
        for e in self._by_id.values()
        if e["kind"] == kind
        and e["enabled"]
        and e["model_id"] != ""
        and (e["stage"] != "testing" or can_use_testing)
    ]
    return sorted(opts, key=lambda o: (o["engine"], o["model_id"]))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/api_gateway && ../../.venv/bin/python -m pytest tests/services/model_registry/test_store_options.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/model_registry/store.py apps/api_gateway/tests/services/model_registry/test_store_options.py
git commit -m "feat(model-registry): add list_options store helper (enabled, non-sentinel, stage-filtered)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Add `GET /v1/model_registry/options?kind=` endpoint

**Files:**
- Modify: `apps/api_gateway/app/api/routes/model_registry.py` (add route + import actor helper)
- Test: `apps/api_gateway/tests/api/test_model_registry_options.py`

**Interfaces:**
- Consumes: `ModelRegistryStore.list_options` (Task 2).
- Produces: `GET /v1/model_registry/options?kind=stt|tts|llm` → `{"success": True, "data": [{"engine","model_id","label"}]}`. `can_use_testing` resolved from the acting user (same pattern as `routes/profiles.py:_resolve_acting_user`, falling back to `False` when no user). Rejects unknown `kind` with HTTP 400.

- [ ] **Step 1: Write the failing test**

```python
# apps/api_gateway/tests/api/test_model_registry_options.py
import pytest


@pytest.mark.asyncio
async def test_options_returns_enabled_entries_for_kind(client, tmp_db):
    from app.services.model_registry.store import model_registry_store
    await model_registry_store.create("stt", "whisper", "tiny", "Tiny", enabled=True)
    await model_registry_store.create("tts", "vieneu", "v3turbo", "VieNeu", enabled=True)

    resp = await client.get("/v1/model_registry/options?kind=stt")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data == [{"engine": "whisper", "model_id": "tiny", "label": "Tiny"}]


@pytest.mark.asyncio
async def test_options_rejects_unknown_kind(client, tmp_db):
    resp = await client.get("/v1/model_registry/options?kind=bogus")
    assert resp.status_code == 400
```

Note: follow the existing test's `client`/`tmp_db` fixture names — check a sibling test in `tests/api/` (e.g. `test_model_registry*.py`) and mirror its fixtures/auth exactly.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/api_gateway && ../../.venv/bin/python -m pytest tests/api/test_model_registry_options.py -v`
Expected: FAIL with 404 (route not defined) / 405.

- [ ] **Step 3: Implement the route**

In `routes/model_registry.py`, add imports at top:
```python
from fastapi import Request
from app.core.actor import current_user_id
from app.services.auth.users import user_store
```
Add the route (place it before `create_entry`, after `get_config_schema`):
```python
_VALID_KINDS = {"stt", "tts", "llm"}


@router.get("/options")
async def list_options(kind: str, request: Request) -> dict:
    """Selectable models for a dropdown, filtered to what this user may pick.
    The single source of truth every profile/service select reads."""
    if kind not in _VALID_KINDS:
        raise HTTPException(status_code=400, detail=f"unknown kind '{kind}'")
    user_id = current_user_id(request)
    user = await user_store.get_by_id(user_id) if user_id else None
    can_use_testing = bool(user and user.can_use_testing)
    options = await model_registry_store.list_options(kind, can_use_testing)
    return {"success": True, "data": options}
```

Confirm `/options` is declared before `PATCH /{entry_id}` so it isn't shadowed by the path param (GET vs PATCH differ, but keep it above to be safe).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/api_gateway && ../../.venv/bin/python -m pytest tests/api/test_model_registry_options.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/api/routes/model_registry.py apps/api_gateway/tests/api/test_model_registry_options.py
git commit -m "feat(model-registry): add GET /v1/model_registry/options?kind= endpoint

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Switch gate to catalog-mode + remove `/v1/profiles/llm-options`

The behavioral heart. Gate now requires an enabled, stage-valid entry (was: no-entry = allowed).

**Files:**
- Modify: `apps/api_gateway/app/services/model_registry/gate.py` (invert missing-entry semantics)
- Modify: `apps/api_gateway/app/api/routes/profiles.py` (remove `list_llm_options` route at :102-113; `_validate_profile_models` keeps calling `check_model_allowed`)
- Test: `apps/api_gateway/tests/services/model_registry/test_gate_catalog_mode.py`

**Interfaces:**
- Consumes: `model_registry_store.find` (existing).
- Produces: `check_model_allowed(kind, engine, model_id, user)` raises `ModelNotAllowedError` when no entry exists for a non-empty `(engine, model_id)`; unchanged behavior for disabled/testing. Empty engine or model_id still returns early (no restriction — inherit-global case).

- [ ] **Step 1: Write the failing test**

```python
# apps/api_gateway/tests/services/model_registry/test_gate_catalog_mode.py
import pytest
from app.core.errors import ModelNotAllowedError
from app.services.model_registry.gate import check_model_allowed
from app.services.model_registry.store import model_registry_store


@pytest.mark.asyncio
async def test_no_entry_now_rejected(tmp_db):
    with pytest.raises(ModelNotAllowedError):
        await check_model_allowed("stt", "whisper", "tiny", None)


@pytest.mark.asyncio
async def test_enabled_entry_allowed(tmp_db):
    await model_registry_store.create("stt", "whisper", "tiny", "Tiny", enabled=True)
    await check_model_allowed("stt", "whisper", "tiny", None)  # no raise


@pytest.mark.asyncio
async def test_empty_selection_is_unrestricted(tmp_db):
    await check_model_allowed("stt", "", "", None)  # inherit-global, no raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/api_gateway && ../../.venv/bin/python -m pytest tests/services/model_registry/test_gate_catalog_mode.py -v`
Expected: `test_no_entry_now_rejected` FAILS (no exception raised).

- [ ] **Step 3: Implement**

In `gate.py`, replace the `if entry is None: return` branch and update the docstring:
```python
async def check_model_allowed(kind: str, engine: str, model_id: str, user: User | None) -> None:
    if not engine or not model_id:
        return
    entry = await model_registry_store.find(kind, engine, model_id)
    if entry is None:
        raise ModelNotAllowedError(
            f"{kind} model '{engine}/{model_id}' is not in the model registry"
        )
    if not entry["enabled"]:
        raise ModelNotAllowedError(f"{kind} model '{engine}/{model_id}' is currently disabled")
    if entry["stage"] == "testing" and not (user and user.can_use_testing):
        raise ModelNotAllowedError(
            f"{kind} model '{engine}/{model_id}' is in testing and not enabled for your account"
        )
```
Update the module docstring's "No matching entry -> unrestricted" sentence to describe catalog-mode.

In `routes/profiles.py`, delete the entire `@router.get("/llm-options")` function (lines 102-113). Leave `_validate_profile_models` untouched.

- [ ] **Step 4: Run gate tests + full profiles/registry suites**

Run: `cd apps/api_gateway && ../../.venv/bin/python -m pytest tests/services/model_registry/ tests/api/ -v`
Expected: new gate tests PASS. Some existing tests that relied on "no entry = allowed" or hit `/v1/profiles/llm-options` will FAIL — fix them: create the needed registry entry in the test's arrange step, or repoint to `/v1/model_registry/options?kind=llm`. Do not weaken the gate to make old tests pass.

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/model_registry/gate.py apps/api_gateway/app/api/routes/profiles.py apps/api_gateway/tests
git commit -m "feat(model-registry): gate becomes catalog-mode; remove /v1/profiles/llm-options

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Auto-sync helpers — `ensure_registry_entry` / `disable_registry_entry`

Pure store-level helpers, TDD'd before wiring into the Models action endpoints.

**Files:**
- Create: `apps/api_gateway/app/services/model_registry/autosync.py`
- Test: `apps/api_gateway/tests/services/model_registry/test_autosync.py`

**Interfaces:**
- Consumes: `model_registry_store.find`, `.create`, `.set_fields`.
- Produces:
  - `async ensure_registry_entry(kind, engine, model_id, label) -> None`: if no entry for `(kind, engine, model_id)` → `create(..., enabled=True, stage="stable")`; if it exists and is disabled → `set_fields(id, enabled=True)`; if it exists and is enabled → no-op. Never touches api_key/base_url/config/stage/label on an existing row.
  - `async disable_registry_entry(kind, engine, model_id) -> None`: if entry exists → `set_fields(id, enabled=False)`; else no-op.

- [ ] **Step 1: Write the failing test**

```python
# apps/api_gateway/tests/services/model_registry/test_autosync.py
import pytest
from app.services.model_registry.autosync import ensure_registry_entry, disable_registry_entry
from app.services.model_registry.store import model_registry_store


@pytest.mark.asyncio
async def test_ensure_creates_enabled_entry(tmp_db):
    await ensure_registry_entry("stt", "whisper", "tiny", "whisper — Tiny")
    entry = await model_registry_store.find("stt", "whisper", "tiny")
    assert entry["enabled"] is True and entry["stage"] == "stable" and entry["label"] == "whisper — Tiny"


@pytest.mark.asyncio
async def test_ensure_reenables_without_clobbering_config(tmp_db):
    created = await model_registry_store.create(
        "stt", "whisper", "tiny", "Custom label", enabled=False,
        api_key="sk-secret", config={"beam_size": 7},
    )
    await ensure_registry_entry("stt", "whisper", "tiny", "whisper — Tiny")
    entry = await model_registry_store.find("stt", "whisper", "tiny")
    assert entry["enabled"] is True
    assert entry["label"] == "Custom label"        # not overwritten
    assert entry["api_key"] == "sk-secret"          # preserved
    assert entry["config"] == {"beam_size": 7}      # preserved


@pytest.mark.asyncio
async def test_disable_keeps_row(tmp_db):
    await model_registry_store.create("stt", "whisper", "tiny", "Tiny", enabled=True, api_key="k")
    await disable_registry_entry("stt", "whisper", "tiny")
    entry = await model_registry_store.find("stt", "whisper", "tiny")
    assert entry is not None and entry["enabled"] is False and entry["api_key"] == "k"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/api_gateway && ../../.venv/bin/python -m pytest tests/services/model_registry/test_autosync.py -v`
Expected: FAIL with `ModuleNotFoundError: app.services.model_registry.autosync`

- [ ] **Step 3: Implement**

```python
# apps/api_gateway/app/services/model_registry/autosync.py
"""Bridge from the Models page (artifact lifecycle) to the Model Registry
(selection source of truth). Installing a local model ensures it has an
enabled registry entry so profiles can pick it; deleting disables that entry
(never removes the row, so an admin's api_key/config survives a reinstall)."""

from __future__ import annotations

from app.services.model_registry.store import model_registry_store


async def ensure_registry_entry(kind: str, engine: str, model_id: str, label: str) -> None:
    entry = await model_registry_store.find(kind, engine, model_id)
    if entry is None:
        await model_registry_store.create(kind, engine, model_id, label, enabled=True, stage="stable")
    elif not entry["enabled"]:
        await model_registry_store.set_fields(entry["id"], enabled=True)


async def disable_registry_entry(kind: str, engine: str, model_id: str) -> None:
    entry = await model_registry_store.find(kind, engine, model_id)
    if entry is not None:
        await model_registry_store.set_fields(entry["id"], enabled=False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/api_gateway && ../../.venv/bin/python -m pytest tests/services/model_registry/test_autosync.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/model_registry/autosync.py apps/api_gateway/tests/services/model_registry/test_autosync.py
git commit -m "feat(model-registry): add ensure/disable autosync helpers (Models -> Registry)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Engine→(kind, model_id, label) mapping + wire auto-sync into Models endpoints

Wire the helpers into the download/select/delete action endpoints in `system.py`.

**Files:**
- Create: `apps/api_gateway/app/services/model_registry/engine_map.py`
- Modify: `apps/api_gateway/app/api/routes/system.py` (call auto-sync after successful download/delete for whisper, vosk, omnivoice, vieneu, llm)
- Test: `apps/api_gateway/tests/api/test_models_autosync.py`

**Interfaces:**
- Consumes: `ensure_registry_entry`, `disable_registry_entry` (Task 5).
- Produces: `registry_ref(page_engine: str, artifact_id: str) -> tuple[str, str, str, str] | None` returning `(kind, engine, model_id, label)` for a Models-page artifact, or `None` if that page-engine has no registry mapping. Mapping:
  - whisper `size` → `("stt", "whisper", size, f"whisper — {size}")`
  - vosk `name` → `("stt", "vosk", name, f"vosk — {name}")`
  - omnivoice `id` → `("tts", "omnivoice", id, f"omnivoice — {id}")`
  - vieneu `mode` → `("tts", "vieneu", mode, f"vieneu — {mode}")`
  - llm `model` → `("llm", "ollama", model, f"ollama — {model}")`

- [ ] **Step 1: Write the failing test**

```python
# apps/api_gateway/tests/api/test_models_autosync.py
import pytest
from app.services.model_registry.store import model_registry_store


@pytest.mark.asyncio
async def test_whisper_download_creates_registry_entry(client, tmp_db, monkeypatch):
    # Don't actually fetch weights — the endpoint queues via BackgroundTasks and
    # calls whisper_manager.validate/download; stub download so the test is fast.
    from app.services.whisper_models import whisper_manager
    monkeypatch.setattr(whisper_manager, "download", lambda size: None)

    resp = await client.post("/v1/models/whisper/download", json={"size": "tiny"})
    assert resp.status_code == 200
    entry = await model_registry_store.find("stt", "whisper", "tiny")
    assert entry is not None and entry["enabled"] is True


@pytest.mark.asyncio
async def test_whisper_delete_disables_registry_entry(client, tmp_db, monkeypatch):
    from app.services.whisper_models import whisper_manager
    monkeypatch.setattr(whisper_manager, "delete", lambda size: None)
    await model_registry_store.create("stt", "whisper", "tiny", "Tiny", enabled=True)

    resp = await client.delete("/v1/models/whisper/tiny")
    assert resp.status_code == 200
    entry = await model_registry_store.find("stt", "whisper", "tiny")
    assert entry["enabled"] is False
```

Check the sibling `tests/api/` fixtures for the real `client`/`tmp_db` names and mirror them.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/api_gateway && ../../.venv/bin/python -m pytest tests/api/test_models_autosync.py -v`
Expected: FAIL (no registry entry created).

- [ ] **Step 3: Implement the map**

```python
# apps/api_gateway/app/services/model_registry/engine_map.py
"""Maps a Models-page artifact (page-engine + its id field) to the Model
Registry coordinates auto-sync should ensure/disable. Local models only —
remote/BYO engines are added directly in the Registry, not via the Models page."""

from __future__ import annotations


def registry_ref(page_engine: str, artifact_id: str) -> tuple[str, str, str, str] | None:
    table = {
        "whisper": ("stt", "whisper"),
        "vosk": ("stt", "vosk"),
        "omnivoice": ("tts", "omnivoice"),
        "vieneu": ("tts", "vieneu"),
        "llm": ("llm", "ollama"),
    }
    ref = table.get(page_engine)
    if ref is None or not artifact_id:
        return None
    kind, engine = ref
    return (kind, engine, artifact_id, f"{engine} — {artifact_id}")
```

- [ ] **Step 4: Wire into `system.py` download/delete endpoints**

Add import near the top of `routes/system.py`:
```python
from app.services.model_registry.autosync import ensure_registry_entry, disable_registry_entry
from app.services.model_registry.engine_map import registry_ref
```
In each download endpoint, after the artifact is queued/validated, `await ensure_registry_entry(*registry_ref(...))`. In each delete endpoint, `await disable_registry_entry(kind, engine, model_id)`. Concretely for whisper:
```python
@router.post("/models/whisper/download")
async def download_whisper(payload: WhisperRequest, background: BackgroundTasks) -> dict:
    whisper_manager.validate(payload.size)
    background.add_task(whisper_manager.download, payload.size)
    ref = registry_ref("whisper", payload.size)
    if ref:
        await ensure_registry_entry(*ref)
    return {"success": True, "data": {"size": payload.size, "state": "queued"}}


@router.delete("/models/whisper/{size}")
async def delete_whisper(size: str) -> dict:
    whisper_manager.delete(size)
    kind, engine, model_id, _ = registry_ref("whisper", size)
    await disable_registry_entry(kind, engine, model_id)
    return {"success": True, "data": {"size": size, "state": "deleted"}}
```
Apply the same pattern to vosk (`payload.name` / path `{name}`), omnivoice (`payload.id`), vieneu (`payload.mode`), and llm (`payload.model`). Ordering note: `ensure_registry_entry` on **download** (not select) so an install shows up immediately.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd apps/api_gateway && ../../.venv/bin/python -m pytest tests/api/test_models_autosync.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/services/model_registry/engine_map.py apps/api_gateway/app/api/routes/system.py apps/api_gateway/tests/api/test_models_autosync.py
git commit -m "feat(model-registry): auto-sync Models install/delete into Registry entries

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Boot-time seed migration — installed + config-referenced models → entries

Ensures nothing already installed or already used by a profile/config vanishes from dropdowns when the gate flips to catalog-mode.

**Files:**
- Modify: `apps/api_gateway/app/services/model_registry/seed.py` (add `seed_installed_models_to_registry`)
- Modify: `apps/api_gateway/app/main.py:129-141` (call it after the existing migrations)
- Test: `apps/api_gateway/tests/services/model_registry/test_seed_installed.py`

**Interfaces:**
- Consumes: `ensure_registry_entry` (Task 5), `whisper_manager.snapshot`, `model_manager.snapshot` (vosk), `profile_store.list`.
- Produces: `async seed_installed_models_to_registry() -> None`, idempotent. For each cached whisper size and installed vosk model → `ensure_registry_entry`. For each profile with a non-empty `stt.model` (and `llm.engine`+`llm.model`) lacking an entry → `ensure_registry_entry` with a label like `f"{engine} — {model_id} (in use)"`. Never disables anything.

- [ ] **Step 1: Write the failing test**

```python
# apps/api_gateway/tests/services/model_registry/test_seed_installed.py
import pytest
from app.services.model_registry.seed import seed_installed_models_to_registry
from app.services.model_registry.store import model_registry_store


@pytest.mark.asyncio
async def test_seeds_cached_whisper_models(tmp_db, monkeypatch):
    from app.services.whisper_models import whisper_manager
    monkeypatch.setattr(whisper_manager, "snapshot", lambda: {
        "models": [
            {"size": "tiny", "label": "Tiny", "cached": True, "active": True},
            {"size": "large-v3", "label": "Large", "cached": False, "active": False},
        ],
        "active": "tiny",
    })
    from app.services.models import model_manager
    monkeypatch.setattr(model_manager, "snapshot", lambda: {"installed": [], "active": None})

    await seed_installed_models_to_registry()
    assert await model_registry_store.find("stt", "whisper", "tiny") is not None
    assert await model_registry_store.find("stt", "whisper", "large-v3") is None  # not cached


@pytest.mark.asyncio
async def test_idempotent_and_preserves_disabled(tmp_db, monkeypatch):
    from app.services.whisper_models import whisper_manager
    from app.services.models import model_manager
    monkeypatch.setattr(whisper_manager, "snapshot", lambda: {
        "models": [{"size": "tiny", "label": "Tiny", "cached": True, "active": True}], "active": "tiny"})
    monkeypatch.setattr(model_manager, "snapshot", lambda: {"installed": [], "active": None})

    await model_registry_store.create("stt", "whisper", "tiny", "Tiny", enabled=False)
    await seed_installed_models_to_registry()
    # ensure_registry_entry re-enables a disabled row — acceptable: it IS installed.
    entry = await model_registry_store.find("stt", "whisper", "tiny")
    assert entry["enabled"] is True
    await seed_installed_models_to_registry()  # second run: no crash, still one row
    all_tiny = [e for e in await model_registry_store.list_all()
                if e["engine"] == "whisper" and e["model_id"] == "tiny"]
    assert len(all_tiny) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/api_gateway && ../../.venv/bin/python -m pytest tests/services/model_registry/test_seed_installed.py -v`
Expected: FAIL with `ImportError: cannot import name 'seed_installed_models_to_registry'`

- [ ] **Step 3: Implement**

Append to `seed.py`:
```python
async def seed_installed_models_to_registry() -> None:
    """Back-fill enabled entries for models that are already installed or already
    referenced by a profile, so flipping the gate to catalog-mode doesn't hide
    anything that currently works. Idempotent; never disables."""
    from app.services.model_registry.autosync import ensure_registry_entry
    from app.services.models import model_manager
    from app.services.profiles.store import profile_store
    from app.services.whisper_models import whisper_manager

    for m in whisper_manager.snapshot()["models"]:
        if m["cached"]:
            await ensure_registry_entry("stt", "whisper", m["size"], f"whisper — {m['label']}")

    for m in model_manager.snapshot()["installed"]:
        await ensure_registry_entry("stt", "vosk", m["name"], f"vosk — {m['name']}")

    for profile in profile_store.list().values():
        if profile.stt.engine and profile.stt.model:
            if await model_registry_store.find("stt", profile.stt.engine, profile.stt.model) is None:
                await ensure_registry_entry(
                    "stt", profile.stt.engine, profile.stt.model,
                    f"{profile.stt.engine} — {profile.stt.model} (in use)")
        if profile.llm.engine and profile.llm.model:
            if await model_registry_store.find("llm", profile.llm.engine, profile.llm.model) is None:
                await ensure_registry_entry(
                    "llm", profile.llm.engine, profile.llm.model,
                    f"{profile.llm.engine} — {profile.llm.model} (in use)")
```

In `main.py`, add to the seed import block (line ~129) and call after `migrate_omnivoice_to_registry()`:
```python
    from app.services.model_registry.seed import seed_installed_models_to_registry
    ...
    await migrate_omnivoice_to_registry()
    await seed_installed_models_to_registry()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/api_gateway && ../../.venv/bin/python -m pytest tests/services/model_registry/test_seed_installed.py -v`
Expected: PASS

- [ ] **Step 5: Full backend suite (catch fallout from the gate flip)**

Run: `cd apps/api_gateway && ../../.venv/bin/python -m pytest -q`
Expected: PASS. Fix any remaining tests that assumed old gate/llm-options behavior.

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/services/model_registry/seed.py apps/api_gateway/app/main.py apps/api_gateway/tests/services/model_registry/test_seed_installed.py
git commit -m "feat(model-registry): seed installed + in-use models into registry on boot

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Playground JS — all model selects read the options endpoint

Repoint every model dropdown to `/v1/model_registry/options?kind=` and drop the LLM `__custom__` path.

**Files:**
- Modify: `apps/api_gateway/app/static/js/profiles.js` (STT + LLM select, load + render + save)
- Modify: `apps/api_gateway/app/static/js/stt-engines.js:21` (`/v1/stt/engines` → options)
- Modify: `apps/api_gateway/app/static/js/conversation.js:166`, `livehost.js:203`, `system-config.js:170` (STT engine lists → options)
- Modify: `apps/api_gateway/app/static/index.html` (LLM: remove the `__custom__` custom-fields block usage; STT select stays)

**Interfaces:**
- Consumes: `GET /v1/model_registry/options?kind=stt|llm` → `[{engine, model_id, label}]`.
- Produces: `pf-stt-model` option values stay `"engine|model_id"`; `readProfileSttSelection()` unchanged. `pf-llm-select` option values become `"engine|model_id"` (was registry-entry `id`); save maps back to `{engine, model, base_url:"", api_key:""}`.

- [ ] **Step 1: Rewrite `loadLlmOptions` + `renderProfileLlmSelect` (profiles.js)**

Replace the fetch URL and drop `__custom__`:
```javascript
export async function loadLlmOptions() {
  try {
    const body = await (await fetch("/v1/model_registry/options?kind=llm")).json();
    llmOptionData = body.data || [];
  } catch {
    llmOptionData = [];
  }
}

export function renderProfileLlmSelect() {
  const sel = el("pf-llm-select");
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = "";
  if (llmOptionData.length === 0) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "(no LLM models — add one in Model Registry)";
    sel.appendChild(opt);
  }
  llmOptionData.forEach((entry) => {
    const opt = document.createElement("option");
    opt.value = `${entry.engine}|${entry.model_id}`;
    opt.textContent = entry.label;
    sel.appendChild(opt);
  });
  if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
}
```
Delete `toggleLlmCustomFields` and its listener at `profiles.js:473`. Options no longer carry `id`, so `entry.id` references are gone.

- [ ] **Step 2: Update LLM load + save (profiles.js)**

In `openProfilePanel`'s edit branch (lines 199-206), replace the matched-option/custom logic:
```javascript
    el("pf-llm-select").value = p.llm?.engine ? `${p.llm.engine}|${p.llm.model || ""}` : "";
```
Remove the `pf-llm-url`/`pf-llm-model`/`pf-llm-key`/`toggleLlmCustomFields()` lines (196-206) for the custom fields, and the `mode === "new"` custom reset (lines 176-179).

In `saveProfile` (lines 273-285), replace the `llm` IIFE:
```javascript
    llm: (() => {
      const raw = el("pf-llm-select")?.value || "";
      const [engine = "", model = ""] = raw ? raw.split("|") : ["", ""];
      return { base_url: "", api_key: "", model, engine };
    })(),
```

- [ ] **Step 3: Rewrite `renderProfileSttModelSelect` (profiles.js) to use options**

```javascript
export async function renderProfileSttModelSelect(selEngine, selModel) {
  const sel = el("pf-stt-model");
  if (!sel) return;
  sel.innerHTML = '<option value="">(inherit global)</option>';
  try {
    const body = await (await fetch("/v1/model_registry/options?kind=stt")).json();
    (body.data || []).forEach((o) => {
      const opt = document.createElement("option");
      opt.value = `${o.engine}|${o.model_id}`;
      opt.textContent = o.label;
      sel.appendChild(opt);
    });
  } catch {
    /* keep just the inherit option */
  }
  const want = selEngine ? `${selEngine}|${selModel || ""}` : "";
  if ([...sel.options].some((o) => o.value === want)) {
    sel.value = want;
  } else if (selEngine) {
    const opt = document.createElement("option");
    opt.value = want;
    opt.textContent = `${selEngine}${selModel ? ` — ${selModel}` : ""} (unavailable)`;
    sel.appendChild(opt);
    sel.value = want;
  }
}
```
`readProfileSttSelection()` is unchanged (still splits on `|`).

- [ ] **Step 4: Update HTML — hide the LLM custom fields block**

In `index.html`, remove or leave-hidden the `#pf-llm-custom-fields` block (lines ~196-208). Simplest: delete the block and its inputs (`pf-llm-url`, `pf-llm-model`, `pf-llm-key`) since LLM is now registry-only. Verify no other JS still reads those ids after Step 2 (grep).

- [ ] **Step 5: Repoint secondary engine selects**

`stt-engines.js:21`, `conversation.js:166`, `livehost.js:203`, `system-config.js:170`: these fetch `/v1/stt/engines` for an engine list. These are engine selectors (not model selectors) used for batch/stream/config. Repoint the **model-choosing** ones to options; leave pure engine-availability displays reading `/v1/stt/engines` if they only need availability/detail. Decision rule per call site: if the value saved is a model choice a user picks, use options; if it's an engine-capability display, keep engines. Document which you changed in the commit message.

- [ ] **Step 6: Manual smoke test in the browser**

Run the app locally (see project run skill), open a profile: STT model dropdown lists only enabled registry entries; LLM dropdown lists only enabled `kind=llm` entries with no "Custom…"; saving and reloading round-trips the selection.

- [ ] **Step 7: Commit**

```bash
git add apps/api_gateway/app/static
git commit -m "feat(ui): profile/engine model selects read Model Registry options

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: lugo-web-client — options endpoint for STT + LLM selects

Submodule (React/TS). Mirror the playground change.

**Files:**
- Modify: `lugo-web-client/src/api/stt.ts` (`listSttModelOptions` → options endpoint)
- Modify: `lugo-web-client/src/api/profiles.ts:86-88` (`listLlmOptions` → options endpoint; `LlmOption` shape)
- Modify: `lugo-web-client/src/screens/ProfileEditor.tsx:99-121` (LLM: single select instead of engine/model datalists)
- Test: `lugo-web-client/src/api/stt.test.ts` (or the repo's existing test file for stt api) + `ProfileEditor` test

**Interfaces:**
- Consumes: `GET /v1/model_registry/options?kind=stt|llm` → `[{engine, model_id, label}]`.
- Produces: `listSttModelOptions(): Promise<SttModelOption[]>` (shape `{engine, model, label}`); `listLlmOptions(): Promise<LlmOption[]>` (shape `{engine, model_id, label}` — drop `id`).

- [ ] **Step 1: Inspect the submodule's test setup**

Run: `cd lugo-web-client && ls src/api/*.test.ts src/screens/*.test.tsx 2>/dev/null; cat package.json | grep -A3 scripts`
Note the test runner command (Vitest) and existing mock pattern for `apiFetch`.

- [ ] **Step 2: Write the failing test for `listSttModelOptions`**

Mirror the existing stt api test's mocking style. Assert that with `apiFetch` mocked to return `{data:[{engine:"whisper",model_id:"tiny",label:"whisper — Tiny"}]}` for `/v1/model_registry/options?kind=stt`, `listSttModelOptions()` resolves to `[{engine:"whisper", model:"tiny", label:"whisper — Tiny"}]` and makes exactly ONE request (no per-engine fan-out).

- [ ] **Step 3: Run to verify it fails**

Run: `cd lugo-web-client && npm test -- stt`
Expected: FAIL (still calls `/v1/stt/engines` + fans out).

- [ ] **Step 4: Rewrite `listSttModelOptions` (stt.ts)**

```typescript
import { apiFetch } from './client'

export interface SttModelOption { engine: string; model: string; label: string }
interface RegistryOption { engine: string; model_id: string; label: string }

export async function listSttModelOptions(): Promise<SttModelOption[]> {
  const resp = await apiFetch('/v1/model_registry/options?kind=stt')
  if (!resp.ok) throw new Error(`Server returned error ${resp.status}`)
  const opts = (((await resp.json()).data ?? []) as RegistryOption[])
  return opts.map((o) => ({ engine: o.engine, model: o.model_id, label: o.label }))
}
```

- [ ] **Step 5: Rewrite `listLlmOptions` (profiles.ts)**

```typescript
export interface LlmOption { engine: string; model_id: string; label: string }

export async function listLlmOptions(): Promise<LlmOption[]> {
  return jsonData<LlmOption[]>(await apiFetch('/v1/model_registry/options?kind=llm'))
}
```

- [ ] **Step 6: Update `ProfileEditor.tsx` LLM block**

Replace the engine/model datalist inputs (lines 99-121) with a single select whose value is `${engine}|${model_id}`, plus keep the Base URL/API key fields removed (registry carries them). On change, split and `patch({ llm: { ...form.llm, engine, model, base_url: '', api_key: '' } })`. Preselect from `form.llm.engine`/`form.llm.model`. If `llmOptions` is empty, show a disabled "(no LLM models — add one in Model Registry)" option.

- [ ] **Step 7: Run submodule tests**

Run: `cd lugo-web-client && npm test`
Expected: PASS. Fix any ProfileEditor test asserting the old datalist markup.

- [ ] **Step 8: Commit the submodule, then bump the pointer in the superproject**

```bash
cd lugo-web-client
git add src/api/stt.ts src/api/profiles.ts src/screens/ProfileEditor.tsx src/api/*.test.ts src/screens/*.test.tsx
git commit -m "feat(web): STT/LLM selects read Model Registry options endpoint

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
git push
cd /Users/lugon/code/speech-text-transformer
git add lugo-web-client
git commit -m "chore(submodule): bump lugo-web-client — registry-options model selects

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

Note: pushing the submodule to its own remote is required before the superproject pointer is meaningful. Do NOT push the superproject `main` until the whole plan is verified (main auto-deploys to prod).

---

### Task 10: Full-suite verification gate

**Files:** none (verification only).

- [ ] **Step 1: Backend full suite**

Run: `cd apps/api_gateway && ../../.venv/bin/python -m pytest -q`
Expected: PASS.

- [ ] **Step 2: Web client full suite**

Run: `cd lugo-web-client && npm test`
Expected: PASS.

- [ ] **Step 3: Local endpoint smoke check**

Start the gateway locally. Verify:
- `GET /v1/model_registry/options?kind=stt` returns enabled entries only.
- `GET /v1/profiles/llm-options` now 404s (removed).
- Install a whisper size via the Models page → it appears in the profile STT dropdown.
- Disable that entry in Model Registry → it disappears from the dropdown; saving a profile with it 400s.

- [ ] **Step 4: Report results**

Summarize pass/fail with the actual test counts and the smoke-check observations. Only after this gate passes is the branch ready to merge to main (which auto-deploys).

## Self-review notes

- Spec coverage: rename (T1), options endpoint incl. llm-options merge (T3, T4), catalog-mode gate (T4), auto-sync install/delete (T5, T6), seed migration (T7), TTS `(engine, model_id)` real pairs — covered by the gate change (T4) + auto-sync labels; TTS profile save path uses engine/model already via registry entries. Playground UI (T8), lugo-web-client (T9), testing (each task + T10).
- TTS granularity note: TTS profile *voice* selection is unchanged; TTS *model* entries are created by auto-sync (omnivoice/vieneu) with real `model_id`. If a later need arises to gate TTS at profile-save on `(engine, model_id)`, that reuses the same `check_model_allowed` — no new code.
- No placeholders: every code step shows real code; fixtures reference existing `tmp_db`/`client` patterns (implementer confirms exact names from siblings in Step 1 of T3/T6/T9).
