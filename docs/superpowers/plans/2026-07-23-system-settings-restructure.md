# System Settings Restructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure the admin "System settings" page from 4 unstyled, unlabeled accordions dumping 42 raw field names into 3 labeled, sub-grouped accordions holding only the 29 fields an operator plausibly tunes without a redeploy — moving the other 13 to env vars, deleting dead ones, and simplifying the STT/TTS engine-resolution chain to `profile config > default` (no query-param override).

**Architecture:** Backend: move 9 deploy-time-static fields (paths/URL/secret/startup flags) from `SystemConfig` (SQLite-backed, admin-editable) to `Settings` (env-var-backed, pydantic-settings); delete 2 dead fields and 2 redundant override fields; merge 3 fields across groups; add per-field `Field(title=, description=, json_schema_extra=)` metadata plus a new `GET /v1/system/config/meta` introspection endpoint. Frontend: rewrite `system-config.js` to render 3 metadata-driven groups with sub-blocks and per-group Save, matching the app's existing "technical readout" CSS design system.

**Tech Stack:** FastAPI + Pydantic v2 (`pydantic-settings` for env config), vanilla JS (no framework, ES modules), plain CSS with custom properties. Python 3.12 venv. pytest for backend tests.

## Global Constraints

- Design spec: `docs/superpowers/specs/2026-07-23-system-settings-restructure-design.md` — every task below implements a section of it; read it first for the "why."
- Commit as `lugondev <lugondev@gmail.com>` (see project convention). Never amend; always new commits.
- Run only the affected test file(s) per task; run the **full backend test suite** in the final task before considering this done (main auto-deploys to prod).
- No query-param engine override anywhere in the profile/config/default chain (`resolve_stt`, and the inline TTS resolution in `conversation.py`/`livehost.py`/`lugo.py`) — this was an explicit, confirmed user decision, not an oversight. `q_language`/`q_model` on `resolve_stt` and the standalone `/v1/stt/transcribe`/`/v1/stt/stream` `engine=` request params are explicitly **out of scope** and must not be touched.
- All new/changed field labels and descriptions are English (matches the rest of the admin UI).
- Backend package root for imports/paths below is `apps/api_gateway/app/...`; tests live at repo-root `tests/unit/...` and `tests/integration/...` (not under `apps/api_gateway/`). Run pytest from the repo root.

---

## Task 1: Add 9 deploy-time settings fields to `Settings`

**Files:**
- Modify: `apps/api_gateway/app/core/settings.py:90-92`
- Test: `tests/unit/test_settings_stt_defaults.py` (new)

**Interfaces:**
- Produces: `settings.ollama_bin`, `settings.warmup_on_startup`, `settings.warmup_startup_timeout_s`, `settings.stt_model_dir`, `settings.vosk_model_base_url`, `settings.stt_stream_sample_rate`, `settings.stt_glossary_path`, `settings.pyannote_vad_model`, `settings.pyannote_auth_token` — all consumed by Tasks 2–4.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_settings_stt_defaults.py
from app.core.settings import Settings


def test_deploy_time_stt_settings_have_expected_defaults():
    s = Settings(_env_file=None)
    assert s.ollama_bin == ""
    assert s.warmup_on_startup is True
    assert s.warmup_startup_timeout_s == 180
    assert s.stt_model_dir == "models/stt"
    assert s.vosk_model_base_url == "https://alphacephei.com/vosk/models"
    assert s.stt_stream_sample_rate == 16000
    assert s.stt_glossary_path == ""
    assert s.pyannote_vad_model == "pyannote/segmentation-3.0"
    assert s.pyannote_auth_token == ""


def test_deploy_time_stt_settings_accept_explicit_overrides():
    s = Settings(
        _env_file=None,
        vosk_model_base_url="https://example.com/models",
        stt_stream_sample_rate=8000,
        pyannote_auth_token="hf_test_token",
    )
    assert s.vosk_model_base_url == "https://example.com/models"
    assert s.stt_stream_sample_rate == 8000
    assert s.pyannote_auth_token == "hf_test_token"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_settings_stt_defaults.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'ollama_bin'`

- [ ] **Step 3: Add the fields to `Settings`**

Edit `apps/api_gateway/app/core/settings.py`, inserting before the `model_config = SettingsConfigDict(...)` line (currently line 92):

```python
    # STT/TTS deployment-time config: read once at process startup/init, never
    # meaningfully "tuned" live -- kept out of the admin-editable SystemConfig
    # on purpose (see docs/superpowers/specs/2026-07-23-system-settings-restructure-design.md).
    ollama_bin: str = ""
    warmup_on_startup: bool = True
    warmup_startup_timeout_s: int = 180
    stt_model_dir: str = "models/stt"
    vosk_model_base_url: str = "https://alphacephei.com/vosk/models"
    stt_stream_sample_rate: int = 16000
    stt_glossary_path: str = ""
    pyannote_vad_model: str = "pyannote/segmentation-3.0"
    pyannote_auth_token: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
```

(i.e. add the 9-field block immediately above the existing `model_config = ...` line, and delete the old bare `model_config = ...` line it replaces.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_settings_stt_defaults.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/core/settings.py tests/unit/test_settings_stt_defaults.py
git commit -m "$(cat <<'EOF'
feat(settings): add 9 deploy-time STT/preprocessing env fields

First step of the System Settings restructure (see design spec) — these
fields aren't consumed anywhere yet, Tasks 2-4 move their SystemConfig
equivalents to read from here instead.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Move `ollama_bin`/`warmup_on_startup`/`warmup_startup_timeout_s` to env

**Files:**
- Modify: `apps/api_gateway/app/services/system_config.py:18-29` (`EngineDefaults`)
- Modify: `apps/api_gateway/app/services/llm_models.py:32-41` (`_ollama_bin`)
- Modify: `apps/api_gateway/app/services/recommend/capabilities.py:120-129` (`_ollama`)
- Modify: `apps/api_gateway/app/main.py:104-165` (`lifespan`)
- Modify: `tests/unit/test_system_config_store.py:64-75` (`test_engine_defaults_have_expected_defaults`)

**Interfaces:**
- Consumes: `settings.ollama_bin`, `settings.warmup_on_startup`, `settings.warmup_startup_timeout_s` (Task 1)
- Produces: `EngineDefaults` now has 5 fields: `default_stt_engine`, `default_tts_engine`, `default_tts_engine_voice`, `extra_warmup_stt_engines`, `extra_warmup_tts_engines` (the last 2 removed in Task 5)

- [ ] **Step 1: Update the failing test first**

Edit `tests/unit/test_system_config_store.py`, replace `test_engine_defaults_have_expected_defaults`:

```python
def test_engine_defaults_have_expected_defaults(tmp_path):
    s = SystemConfigStore(str(tmp_path / "system_config.json"))
    e = s.get().engines
    assert e.default_stt_engine == "vosk"
    assert e.default_tts_engine == "omnivoice"
    assert e.default_tts_engine_voice == ""
    assert e.extra_warmup_stt_engines == ""
    assert e.extra_warmup_tts_engines == ""
    assert not hasattr(e, "warmup_on_startup")
    assert not hasattr(e, "warmup_startup_timeout_s")
    assert not hasattr(e, "ollama_bin")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_system_config_store.py::test_engine_defaults_have_expected_defaults -v`
Expected: FAIL — `assert not hasattr(e, "warmup_on_startup")` fails because the field still exists.

- [ ] **Step 3: Remove the 3 fields from `EngineDefaults`**

Edit `apps/api_gateway/app/services/system_config.py`:

```python
class EngineDefaults(BaseModel):
    default_stt_engine: str = "vosk"
    default_tts_engine: str = "omnivoice"
    default_tts_engine_voice: str = ""  # optional VieNeu preset voice
    extra_warmup_stt_engines: str = ""
    extra_warmup_tts_engines: str = ""
```

(deletes `warmup_on_startup: bool = True`, `warmup_startup_timeout_s: int = 180`, and `ollama_bin: str = ""` plus its preceding comment.)

- [ ] **Step 4: Update `_ollama_bin()` in `llm_models.py`**

Edit `apps/api_gateway/app/services/llm_models.py`:

```python
from app.core.errors import AppError
from app.core.settings import settings
from app.services.conversation.responder import (
    _active_llm_entry,
    get_active_llm_model,
    set_active_llm_config,
)

logger = logging.getLogger(__name__)


def _ollama_bin() -> str | None:
    candidates = [
        settings.ollama_bin,
        shutil.which("ollama"),
        "/opt/homebrew/opt/ollama/bin/ollama",
    ]
    for c in candidates:
        if c and (shutil.which(c) or os.path.isfile(c)):
            return c
    return None
```

(replaces the `from app.services.system_config import system_config_store` import with `from app.core.settings import settings`, and `system_config_store.get().engines.ollama_bin` with `settings.ollama_bin`.)

- [ ] **Step 5: Update `_ollama()` in `capabilities.py`**

Edit `apps/api_gateway/app/services/recommend/capabilities.py`, the `_ollama` function body only (leave the `system_config_store` import in place — Task 3 also uses it in this same file and will remove the import there):

```python
def _ollama() -> bool:
    try:
        from app.core.settings import settings

        ollama_bin = settings.ollama_bin
        if ollama_bin and os.path.exists(ollama_bin):
            return True
        return shutil.which("ollama") is not None or os.path.exists(
            "/opt/homebrew/opt/ollama/bin/ollama"
        )
    except Exception:  # noqa: BLE001
        return False
```

- [ ] **Step 6: Update `main.py`'s startup warm-up gating**

Edit `apps/api_gateway/app/main.py`, replace lines 156-164:

```python
    # Warm engines BEFORE the app starts serving so the very first device turn is
    # instant instead of paying a cold model load (worse with connect-on-wake,
    # where the session starts the moment the user wakes). Capped so a stuck/slow
    # warm can't block startup forever (health checks); on timeout we serve cold.
    if settings.warmup_on_startup:
        try:
            await asyncio.wait_for(_warm_default_engines(), timeout=settings.warmup_startup_timeout_s)
        except TimeoutError:
            logger.warning(
                "boot warm-up exceeded %ss — serving anyway; the first turn may be cold",
                settings.warmup_startup_timeout_s,
            )
```

(drops the `engine_defaults = system_config_store.get().engines` line and reads `settings.warmup_on_startup`/`settings.warmup_startup_timeout_s` directly — `settings` is already imported at the top of `main.py`.)

- [ ] **Step 7: Run the test to verify it passes**

Run: `pytest tests/unit/test_system_config_store.py::test_engine_defaults_have_expected_defaults -v`
Expected: PASS

- [ ] **Step 8: Run the broader affected test files**

Run: `pytest tests/unit/test_system_config_store.py tests/unit/test_system_config_routes.py tests/unit/test_warmup_engine_settings.py tests/unit/test_settings_stt_defaults.py -v`
Expected: all PASS (no other test currently asserts on `ollama_bin`/`warmup_on_startup`/`warmup_startup_timeout_s` per the earlier repo-wide grep, so nothing else should break)

- [ ] **Step 9: Commit**

```bash
git add apps/api_gateway/app/services/system_config.py apps/api_gateway/app/services/llm_models.py apps/api_gateway/app/services/recommend/capabilities.py apps/api_gateway/app/main.py tests/unit/test_system_config_store.py
git commit -m "$(cat <<'EOF'
refactor(config): move ollama_bin/warmup_on_startup/warmup_startup_timeout_s to env

These are read once at process startup/capability-check time, never live-
tuned -- move them off the admin-editable SystemConfig onto Settings (env
vars) per docs/superpowers/specs/2026-07-23-system-settings-restructure-design.md.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Move `stt_model_dir`/`vosk_model_base_url`/`stt_stream_sample_rate`/`stt_glossary_path` to env

**Files:**
- Modify: `apps/api_gateway/app/services/system_config.py:32-44` (`SttLocalConfig`)
- Modify: `apps/api_gateway/app/services/models.py:1-38,88-94` (`ModelManager`)
- Modify: `apps/api_gateway/app/services/recommend/capabilities.py:1-20,171-172` (`detect_capabilities`)
- Modify: `apps/api_gateway/app/api/routes/system.py:50-89` (`system_status`)
- Modify: `apps/api_gateway/app/api/routes/conversation.py:248`
- Modify: `apps/api_gateway/app/api/routes/lugo.py:109`
- Modify: `apps/api_gateway/app/api/routes/livehost.py:132`
- Modify: `apps/api_gateway/app/api/routes/stt.py:167-170`
- Modify: `apps/api_gateway/app/services/stt/providers/whisper_mlx_provider.py:59`
- Modify: `apps/api_gateway/app/services/stt/providers/whisper_provider.py:99`
- Modify: `tests/unit/test_system_config_store.py:77-87` (`test_stt_local_config_has_expected_defaults`)

**Interfaces:**
- Consumes: `settings.stt_model_dir`, `settings.vosk_model_base_url`, `settings.stt_stream_sample_rate`, `settings.stt_glossary_path` (Task 1)
- Produces: `SttLocalConfig` now has 3 fields left: `stt_segment_long_enabled`, `stt_segment_min_seconds`, `stt_segment_concurrency` (the group itself is deleted in Task 6)

- [ ] **Step 1: Update the failing test first**

Edit `tests/unit/test_system_config_store.py`, replace `test_stt_local_config_has_expected_defaults`:

```python
def test_stt_local_config_has_expected_defaults(tmp_path):
    s = SystemConfigStore(str(tmp_path / "system_config.json"))
    c = s.get().stt_local
    assert not hasattr(c, "stt_model_dir")
    assert not hasattr(c, "vosk_model_base_url")
    assert not hasattr(c, "stt_stream_sample_rate")
    assert not hasattr(c, "stt_glossary_path")
    assert not hasattr(c, "stt_profile")  # preset layer removed
    assert c.stt_segment_long_enabled is False
    assert c.stt_segment_min_seconds == 30.0
    assert c.stt_segment_concurrency == 4
```

Also add to `tests/unit/test_settings_stt_defaults.py` (extends Task 1's file — these 4 fields are already covered by the existing `test_deploy_time_stt_settings_have_expected_defaults`, so no new test needed there; this step is just the `test_stt_local_config_has_expected_defaults` rewrite above).

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_system_config_store.py::test_stt_local_config_has_expected_defaults -v`
Expected: FAIL — `assert not hasattr(c, "stt_model_dir")` fails, field still exists.

- [ ] **Step 3: Remove the 4 fields from `SttLocalConfig`**

Edit `apps/api_gateway/app/services/system_config.py`:

```python
class SttLocalConfig(BaseModel):
    """Engine-agnostic STT settings only. Per-engine settings (default model,
    model path, whisper decode tuning, device/compute_type) live in the Model
    Registry model_id="" sentinel rows -- see
    app/services/model_registry/resolve.py. Model dir / download URL / stream
    sample rate / glossary path are deployment-time constants read once at
    startup -- see app.core.settings.Settings instead."""

    stt_segment_long_enabled: bool = False
    stt_segment_min_seconds: float = 30.0
    stt_segment_concurrency: int = 4
```

- [ ] **Step 4: Update `ModelManager` in `models.py`**

Edit `apps/api_gateway/app/services/models.py`, imports and the two call sites:

```python
from app.core.errors import AppError
from app.core.hf_cache import dir_size_bytes
from app.core.settings import settings
```

(replaces `from app.services.system_config import system_config_store`)

```python
class ModelManager:
    def __init__(self) -> None:
        self._base = Path(settings.stt_model_dir)
```

(replaces `Path(system_config_store.get().stt_local.stt_model_dir)`)

```python
            url = f"{settings.vosk_model_base_url.rstrip('/')}/{name}.zip"
```

(replaces `f"{system_config_store.get().stt_local.vosk_model_base_url.rstrip('/')}/{name}.zip"`)

- [ ] **Step 5: Update `capabilities.py`**

Edit `apps/api_gateway/app/services/recommend/capabilities.py`, drop the now-fully-unused import and update `detect_capabilities()`:

```python
from app.core.deps import module_available
```

(replaces `from app.core.deps import module_available` + `from app.services.system_config import system_config_store` — the latter is now unused in this file after this step, since Task 2 already localized its one use inside `_ollama()`)

```python
        disk_free_gb=_disk_free_gb(settings.stt_model_dir),
```

Add `from app.core.settings import settings` near the top of `detect_capabilities()` (or at module level next to the other imports — module level is simpler and matches the file's existing style):

```python
from app.core.deps import module_available
from app.core.settings import settings
```

(replaces `disk_free_gb=_disk_free_gb(system_config_store.get().stt_local.stt_model_dir)`)

- [ ] **Step 6: Update `system_status()` in `system.py`**

Edit `apps/api_gateway/app/api/routes/system.py`:

```python
@router.get("/system/status")
async def system_status() -> dict:
    from app.services.model_registry.resolve import resolve_omnivoice_config, resolve_stt_local_device
    from app.services.stt.providers.vosk_provider import get_active_vosk_path

    active_vosk_path = get_active_vosk_path()
    active_whisper = whisper_manager.snapshot()["active"]
    preprocessing = system_config_store.get().preprocessing
    omnivoice = resolve_omnivoice_config()
    whisper_device_cfg = resolve_stt_local_device("whisper_local")
    data = {
        "app": {"name": settings.app_name, "env": settings.app_env},
        "stt_engines": await stt_service.list_engines(),
        "tts_engines": tts_service.list_engines(),
        "tts": {
            "omnivoice_path": omnivoice.omnivoice_path,
            "omnivoice_present": os.path.isdir(omnivoice.omnivoice_path),
        },
        "whisper_local": {
            "active_model": active_whisper,
            "device": whisper_device_cfg["device"],
            "cached": whisper_manager._cached(active_whisper),
        },
        "vosk": {
            "active_model_path": active_vosk_path,
            "active_model_present": os.path.isdir(active_vosk_path),
            "installed": model_manager.list_installed(),
        },
        "artifacts": _artifacts_stats(),
        "stream_sample_rate": settings.stt_stream_sample_rate,
        "stt_preprocess": {
            "vad": preprocessing.stt_vad_enabled,
            "vad_backend": preprocessing.stt_vad_backend,
            "vad_backends_available": available_backends(),
            "noise_reduce": preprocessing.stt_noise_reduce_enabled,
            "noise_reduce_amount": preprocessing.stt_noise_reduce_amount,
        },
    }
    return {"success": True, "data": data}
```

(drops the `stt_local = system_config_store.get().stt_local` line, replaces `stt_local.stt_stream_sample_rate` with `settings.stt_stream_sample_rate`; `settings` is already imported at the top of this file)

- [ ] **Step 7: Update the 3 WS routes' sample-rate default**

In each of `apps/api_gateway/app/api/routes/conversation.py:248`, `apps/api_gateway/app/api/routes/lugo.py:109`, `apps/api_gateway/app/api/routes/livehost.py:132`, replace the sample-rate line. `conversation.py`/`livehost.py` (identical pattern):

```python
    sample_rate = int(q.get("sample_rate", settings.stt_stream_sample_rate))
```

(replaces `int(q.get("sample_rate", system_config_store.get().stt_local.stt_stream_sample_rate))`; add `from app.core.settings import settings` to each file's imports if not already present — check with `grep -n "^from app.core.settings" apps/api_gateway/app/api/routes/conversation.py apps/api_gateway/app/api/routes/livehost.py apps/api_gateway/app/api/routes/lugo.py` first and only add where missing)

`lugo.py` (different variable name, `default_sample_rate`):

```python
    default_sample_rate = settings.stt_stream_sample_rate
```

(replaces `system_config_store.get().stt_local.stt_stream_sample_rate`)

- [ ] **Step 8: Update `stt.py`'s raw stream endpoint**

Edit `apps/api_gateway/app/api/routes/stt.py:167-170` (this is the standalone `/v1/stt/stream` endpoint's own sample-rate default — explicitly in scope here since it's the *sample rate* default, not the *engine* query param, which stays untouched per the Global Constraints):

```python
    sample_rate = int(
        websocket.query_params.get(
            "sample_rate", settings.stt_stream_sample_rate
        )
    )
```

Add `from app.core.settings import settings` to `stt.py`'s imports (not currently imported there).

- [ ] **Step 9: Update the 2 whisper providers' glossary path**

Edit `apps/api_gateway/app/services/stt/providers/whisper_mlx_provider.py:56-60` and `apps/api_gateway/app/services/stt/providers/whisper_provider.py:96-100` — both have the identical pattern:

```python
            initial_prompt=resolve_initial_prompt(
                engine_cfg["initial_prompt"],
                settings.stt_glossary_path,
            ),
```

(replaces `system_config_store.get().stt_local.stt_glossary_path`; add `from app.core.settings import settings` to each file's imports — check first whether `system_config_store` is still used elsewhere in either file before removing its import; if not, remove it)

- [ ] **Step 10: Run test to verify it passes**

Run: `pytest tests/unit/test_system_config_store.py::test_stt_local_config_has_expected_defaults -v`
Expected: PASS

- [ ] **Step 11: Run the broader affected test files**

Run: `pytest tests/unit/test_system_config_store.py tests/unit/test_system_config_routes.py tests/unit/test_settings_stt_defaults.py tests/integration/test_conversation_ws.py tests/integration/test_livehost_ws_voice.py -v`
Expected: all PASS. `test_system_config_routes.py::test_get_config_includes_nested_groups_with_defaults` and `::test_put_updates_a_nested_field_and_preserves_others` still reference `data["stt_local"]["stt_model_dir"]` at this point in the plan — **leave those two assertions alone for now**, they'll still pass because `stt_local` still exists as a group (with only the 3 segment fields left) until Task 6 deletes it; only if this pytest run shows them failing because `stt_model_dir` specifically no longer exists in the dumped `stt_local` dict, fix those two lines now to reference `data["engines"]["default_stt_engine"]` instead (pulling that assertion forward from Task 6 is fine and harmless).

- [ ] **Step 12: Commit**

```bash
git add apps/api_gateway/app/services/system_config.py apps/api_gateway/app/services/models.py apps/api_gateway/app/services/recommend/capabilities.py apps/api_gateway/app/api/routes/system.py apps/api_gateway/app/api/routes/conversation.py apps/api_gateway/app/api/routes/lugo.py apps/api_gateway/app/api/routes/livehost.py apps/api_gateway/app/api/routes/stt.py apps/api_gateway/app/services/stt/providers/whisper_mlx_provider.py apps/api_gateway/app/services/stt/providers/whisper_provider.py tests/unit/test_system_config_store.py tests/unit/test_system_config_routes.py
git commit -m "$(cat <<'EOF'
refactor(config): move stt_model_dir/vosk_model_base_url/stt_stream_sample_rate/stt_glossary_path to env

Deployment-time paths/URL/constant, read once at init -- move off the
admin-editable SystemConfig onto Settings (env vars).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Move `pyannote_vad_model`/`pyannote_auth_token` to env; delete now-dead secret-masking code

**Files:**
- Modify: `apps/api_gateway/app/services/system_config.py:126-132` (`PreprocessingConfig`)
- Modify: `apps/api_gateway/app/services/vad.py:14-37,75-86` (`available_backends`, `_pyannote_regions`)
- Modify: `apps/api_gateway/app/api/routes/system.py` (`_mask_system_config`, `_merge_system_config`, PUT handler diff/cache-clear trigger)
- Modify: `tests/unit/test_system_config_store.py:152-160` (`test_preprocessing_config_has_expected_defaults`)
- Modify: `tests/unit/test_system_config_routes.py` (delete 3 pyannote-cache tests + the secret-masking test)

**Interfaces:**
- Consumes: `settings.pyannote_vad_model`, `settings.pyannote_auth_token` (Task 1)
- Produces: `PreprocessingConfig` now has 4 fields: `stt_vad_enabled`, `stt_vad_backend`, `stt_noise_reduce_enabled`, `stt_noise_reduce_amount`. `get_system_config`/`set_system_config` in `system.py` no longer mask/merge anything — `pyannote_auth_token` was the only secret field in `SystemConfig`.

- [ ] **Step 1: Update the failing tests first**

Edit `tests/unit/test_system_config_store.py`, replace `test_preprocessing_config_has_expected_defaults`:

```python
def test_preprocessing_config_has_expected_defaults(tmp_path):
    s = SystemConfigStore(str(tmp_path / "system_config.json"))
    c = s.get().preprocessing
    assert c.stt_vad_enabled is False
    assert c.stt_vad_backend == "energy"
    assert c.stt_noise_reduce_enabled is False
    assert c.stt_noise_reduce_amount == 0.85
    assert not hasattr(c, "pyannote_vad_model")
    assert not hasattr(c, "pyannote_auth_token")
```

Edit `tests/unit/test_system_config_routes.py`: delete these 4 test functions entirely (the behaviors they test no longer exist — `pyannote_vad_model`/`pyannote_auth_token` leave `SystemConfig`, so there's no PUT-time diff to trigger a cache clear, and no secret field left to mask):
- `test_secret_field_is_masked_and_blank_put_preserves_it`
- `test_changing_pyannote_vad_model_clears_the_pyannote_cache`
- `test_changing_pyannote_auth_token_clears_the_pyannote_cache`
- `test_unrelated_field_change_does_not_clear_pyannote_cache`

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_system_config_store.py::test_preprocessing_config_has_expected_defaults -v`
Expected: FAIL — `assert not hasattr(c, "pyannote_vad_model")` fails, field still exists.

- [ ] **Step 3: Remove the 2 fields from `PreprocessingConfig`**

Edit `apps/api_gateway/app/services/system_config.py`:

```python
class PreprocessingConfig(BaseModel):
    stt_vad_enabled: bool = False
    stt_vad_backend: str = "energy"
    stt_noise_reduce_enabled: bool = False
    stt_noise_reduce_amount: float = 0.85
```

- [ ] **Step 4: Update `vad.py`**

Edit `apps/api_gateway/app/services/vad.py`:

```python
from app.core.audio import vad_gate
from app.core.deps import module_available
from app.core.settings import settings
```

(add `from app.core.settings import settings`; `system_config_store` import stays — it's still used nowhere else in this file after this change, so remove `from app.services.system_config import system_config_store` too since it becomes fully unused)

```python
def available_backends() -> dict[str, bool]:
    torch_ok = module_available("torch")
    # pyannote's default VAD pipeline is gated on HF -> needs an auth token.
    pyannote_ok = (
        torch_ok
        and module_available("pyannote.audio")
        and bool(settings.pyannote_auth_token)
    )
    return {
        "energy": True,
        "silero": torch_ok and module_available("silero_vad"),
        "pyannote": pyannote_ok,
    }
```

```python
def _pyannote_regions(samples: np.ndarray, sample_rate: int) -> list[tuple[int, int]]:
    import torch
    from pyannote.audio import Model
    from pyannote.audio.pipelines import VoiceActivityDetection

    if "pipeline" not in _pyannote_cache:
        token = settings.pyannote_auth_token or True
        # segmentation-3.0 is a Model; wrap it in the VAD pipeline (pyannote 3.1/4.x way).
        model = Model.from_pretrained(settings.pyannote_vad_model, token=token)
        pipeline = VoiceActivityDetection(segmentation=model)
        pipeline.instantiate({"min_duration_on": 0.0, "min_duration_off": 0.0})
        _pyannote_cache["pipeline"] = pipeline
    waveform = torch.from_numpy(np.asarray(samples, dtype=np.float32)).unsqueeze(0)
    annotation = _pyannote_cache["pipeline"]({"waveform": waveform, "sample_rate": sample_rate})
    return [
        (int(seg.start * sample_rate), int(seg.end * sample_rate))
        for seg in annotation.get_timeline().support()
    ]
```

(`clear_pyannote_cache()` stays unchanged — it's still a valid function, just no longer auto-triggered by a config PUT; a deploy that changes `PYANNOTE_VAD_MODEL`/`PYANNOTE_AUTH_TOKEN` requires a process restart, which naturally empties `_pyannote_cache`)

- [ ] **Step 5: Simplify `system.py`'s config GET/PUT handlers**

Edit `apps/api_gateway/app/api/routes/system.py`, delete `_mask_system_config` and `_merge_system_config` entirely, and simplify the routes:

```python
@router.get("/system/config")
async def get_system_config() -> dict:
    return {"success": True, "data": system_config_store.get().model_dump()}


@router.put("/system/config")
async def set_system_config(request: Request) -> dict:
    current = system_config_store.get()
    # Accept the raw JSON body (rather than a typed SystemConfig) so we can tell
    # "field absent from the PUT body" apart from "field present with its
    # Pydantic default value" -- a partial body (e.g. the base-context save
    # button, which only ever sends that 1 field) must never reset the other
    # groups back to their hard-coded defaults.
    try:
        raw = await request.json()
        if not isinstance(raw, dict):
            raise ValueError("request body must be a JSON object")
        deep_merged = _deep_merge(current.model_dump(), raw)
        payload = SystemConfig.model_validate(deep_merged)
    except (ValueError, pydantic.ValidationError) as exc:
        # Mirror the structured 422 FastAPI would give automatically for a typed
        # `payload: SystemConfig` parameter -- manual json()/model_validate() calls
        # don't get that for free, so surface the same status/shape ourselves
        # instead of letting a malformed body fall through as a bare 500.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    new_config = system_config_store.set(payload)
    return {"success": True, "data": new_config.model_dump()}
```

(removes: the `_mask_system_config` function, the `_merge_system_config` function, the `merged = _merge_system_config(current, payload)` call, and the `if current.preprocessing.pyannote_vad_model != ... clear_pyannote_cache()` block and its `from app.services.vad import clear_pyannote_cache` local import. `_deep_merge` stays unchanged — still needed for the partial-PUT-preserves-siblings behavior. `current` is still used, just for the deep-merge base, not for the removed diff check.)

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pytest tests/unit/test_system_config_store.py::test_preprocessing_config_has_expected_defaults tests/unit/test_system_config_routes.py -v`
Expected: PASS (the 4 deleted tests no longer run; remaining tests pass — `test_get_config_defaults_empty`, `test_set_config_base_context`, `test_set_config_clears_base_context`, `test_partial_put_does_not_reset_unrelated_group_to_defaults`, `test_malformed_field_type_returns_422_not_500`, `test_non_dict_json_body_returns_422_not_500` don't touch the removed fields/masking logic)

- [ ] **Step 7: Run the wider affected suite**

Run: `pytest tests/unit/test_vad*.py tests/unit/test_system_config_store.py tests/unit/test_system_config_routes.py -v`
Expected: all PASS. If a `test_vad*.py` file references `system_config_store` for pyannote config (grep first: `grep -rln "pyannote" tests/unit/test_vad*.py`), update it to monkeypatch `settings.pyannote_auth_token`/`settings.pyannote_vad_model` instead, following the exact same fix pattern as this task's other edits.

- [ ] **Step 8: Commit**

```bash
git add apps/api_gateway/app/services/system_config.py apps/api_gateway/app/services/vad.py apps/api_gateway/app/api/routes/system.py tests/unit/test_system_config_store.py tests/unit/test_system_config_routes.py
git commit -m "$(cat <<'EOF'
refactor(config): move pyannote_vad_model/pyannote_auth_token to env

Deployment-time model id + secret, chosen once when VAD backend is set up
-- move off the admin-editable SystemConfig onto Settings (env vars).
This was the only secret field in SystemConfig, so the mask/merge
machinery in system.py's GET/PUT handlers is now dead code and removed
too; a config change to either field now requires a process restart,
which already empties the pyannote pipeline cache naturally.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Delete dead `extra_warmup_stt_engines`/`extra_warmup_tts_engines` fields

**Files:**
- Modify: `apps/api_gateway/app/services/system_config.py:18-23,251-268` (`EngineDefaults`, `warmup_stt_engines`, `warmup_tts_engines`)
- Modify: `apps/api_gateway/app/main.py:43-51` (`_warm_default_engines` docstring)
- Modify: `tests/unit/test_system_config_store.py:64-73,178-189` (`test_engine_defaults_have_expected_defaults`, `test_warmup_stt_engines_combines_conversation_default_and_extras`)
- Modify: `tests/unit/test_warmup_engine_settings.py`

**Interfaces:**
- Produces: `EngineDefaults` down to 3 fields: `default_stt_engine`, `default_tts_engine`, `default_tts_engine_voice`. `warmup_stt_engines()`/`warmup_tts_engines()` each return a 0-or-1-element list (still sourced from `conversation.conversation_stt_engine`/`conversation_tts_engine` at the end of this task — Task 7 switches the source to `engines.default_stt_engine`/`default_tts_engine`).

- [ ] **Step 1: Update the failing tests first**

Edit `tests/unit/test_system_config_store.py`, replace `test_engine_defaults_have_expected_defaults` (already edited once in Task 2 — this is a second edit on top of that):

```python
def test_engine_defaults_have_expected_defaults(tmp_path):
    s = SystemConfigStore(str(tmp_path / "system_config.json"))
    e = s.get().engines
    assert e.default_stt_engine == "vosk"
    assert e.default_tts_engine == "omnivoice"
    assert e.default_tts_engine_voice == ""
    assert not hasattr(e, "extra_warmup_stt_engines")
    assert not hasattr(e, "extra_warmup_tts_engines")
```

Delete `test_warmup_stt_engines_combines_conversation_default_and_extras` entirely (tests the exact "extras" behavior being removed).

Edit `tests/unit/test_warmup_engine_settings.py`, replace the whole file's top section (the `_patch_extras` helper and the 5 `test_warmup_*`/`test_boot_warmup_*` functions that use it) — keep the class/`_fake_profile`/`_fake_tts_profile` helpers unchanged, only rewrite the top:

```python
from app.services import system_config as sc_mod
from app.services.system_config import SystemConfigStore


def _patch_conversation_engines(monkeypatch, tmp_path, *, stt_engine="whisper", tts_engine="vieneu"):
    """conversation_stt_engine/conversation_tts_engine live on
    system_config_store's ``conversation`` group -- build a fresh, isolated
    store and patch it in at the point of use (app.services.system_config),
    following the pattern in tests/unit/test_stt_service_openrouter.py."""
    fresh = SystemConfigStore(str(tmp_path / "system_config.json"))
    fresh.set(
        fresh.get().model_copy(
            update={
                "conversation": fresh.get().conversation.model_copy(
                    update={"conversation_stt_engine": stt_engine, "conversation_tts_engine": tts_engine}
                ),
            }
        )
    )
    monkeypatch.setattr(sc_mod, "system_config_store", fresh)


def test_warmup_stt_engines_returns_the_conversation_engine(monkeypatch, tmp_path):
    _patch_conversation_engines(monkeypatch, tmp_path, stt_engine="whisper")
    assert sc_mod.warmup_stt_engines() == ["whisper"]


def test_warmup_tts_engines_returns_the_conversation_engine(monkeypatch, tmp_path):
    _patch_conversation_engines(monkeypatch, tmp_path, tts_engine="vieneu")
    assert sc_mod.warmup_tts_engines() == ["vieneu"]


# --- boot warm-up enumerates every profile / tts-profile engine ---
from app.services import warmup  # noqa: E402
```

(this drops `test_warmup_stt_engines_includes_extra_engines_a_device_pins` and `test_warmup_stt_engines_dedupes_and_strips_whitespace` and `test_warmup_tts_engines_includes_extra_engines` entirely, and keeps `test_boot_warmup_includes_profile_and_tts_profile_engines`/`test_boot_warmup_collects_profile_stt_models` below unchanged for now — they already call `_patch_extras`, so rename those two calls to `_patch_conversation_engines` too, dropping any `stt=`/`tts=` kwargs they pass)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_system_config_store.py::test_engine_defaults_have_expected_defaults tests/unit/test_warmup_engine_settings.py -v`
Expected: FAIL (fields/functions referenced don't match current code yet)

- [ ] **Step 3: Remove the 2 fields and simplify the warm-up functions**

Edit `apps/api_gateway/app/services/system_config.py`:

```python
class EngineDefaults(BaseModel):
    default_stt_engine: str = "vosk"
    default_tts_engine: str = "omnivoice"
    default_tts_engine_voice: str = ""  # optional VieNeu preset voice
```

```python
def warmup_stt_engines() -> list[str]:
    engine = system_config_store.get().conversation.conversation_stt_engine
    return [engine] if engine else []


def warmup_tts_engines() -> list[str]:
    engine = system_config_store.get().conversation.conversation_tts_engine
    return [engine] if engine else []
```

- [ ] **Step 4: Update `main.py`'s docstring**

Edit `apps/api_gateway/app/main.py:43-51`:

```python
async def _warm_default_engines() -> None:
    """Load the STT/TTS engines conversations actually use, at process boot instead
    of waiting for the first WebSocket connect. Covers conversation_stt_engine /
    conversation_tts_engine, plus every engine any chatllm/TTS profile can select
    (see app.services.warmup.engines_for_boot_warmup) -- so a device connecting
    with any profile never pays a cold model load on its first turn."""
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_system_config_store.py tests/unit/test_warmup_engine_settings.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/services/system_config.py apps/api_gateway/app/main.py tests/unit/test_system_config_store.py tests/unit/test_warmup_engine_settings.py
git commit -m "$(cat <<'EOF'
refactor(config): delete dead extra_warmup_stt_engines/extra_warmup_tts_engines

Zero read call sites (grep-confirmed) -- only reference was a stale
comment. Their entire purpose (covering a device that pins an engine via
a query param, which profile-enumeration warm-up can't see) is also about
to become moot once query-param engine overrides are removed (next task).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Merge `stt_segment_*` into `EngineDefaults`; delete `SttLocalConfig`/`stt_local`

**Files:**
- Modify: `apps/api_gateway/app/services/system_config.py` (`EngineDefaults`, delete `SttLocalConfig`, `SystemConfig`)
- Modify: `apps/api_gateway/app/api/routes/stt.py:71-82`
- Modify: `apps/api_gateway/app/api/routes/system.py` (docs/api.md examples not needed here — see Task 10)
- Modify: `tests/unit/test_system_config_store.py` (delete `test_system_config_has_no_stt_local_device_fields`, `test_stt_local_has_no_per_engine_model_or_tuning_fields`; fold segment assertions into `test_engine_defaults_have_expected_defaults`)
- Modify: `tests/unit/test_system_config_routes.py:39-44,47-53` (`test_get_config_includes_nested_groups_with_defaults`, `test_put_updates_a_nested_field_and_preserves_others`)

**Interfaces:**
- Produces: `EngineDefaults` final shape (6 fields): `default_stt_engine`, `default_tts_engine`, `default_tts_engine_voice`, `stt_segment_long_enabled`, `stt_segment_min_seconds`, `stt_segment_concurrency`. `SystemConfig` no longer has a `stt_local` attribute.

- [ ] **Step 1: Update the failing tests first**

Edit `tests/unit/test_system_config_store.py`:

Delete these two tests entirely (their premise — that `stt_local` exists but lacks certain legacy fields — no longer holds once `stt_local` is gone):
- `test_system_config_has_no_stt_local_device_fields`
- `test_stt_local_has_no_per_engine_model_or_tuning_fields`

Delete `test_stt_local_config_has_expected_defaults` entirely (the group it tests is gone) and fold its remaining 3 assertions into `test_engine_defaults_have_expected_defaults`:

```python
def test_engine_defaults_have_expected_defaults(tmp_path):
    s = SystemConfigStore(str(tmp_path / "system_config.json"))
    e = s.get().engines
    assert e.default_stt_engine == "vosk"
    assert e.default_tts_engine == "omnivoice"
    assert e.default_tts_engine_voice == ""
    assert not hasattr(e, "extra_warmup_stt_engines")
    assert not hasattr(e, "extra_warmup_tts_engines")
    assert e.stt_segment_long_enabled is False
    assert e.stt_segment_min_seconds == 30.0
    assert e.stt_segment_concurrency == 4


def test_system_config_has_no_stt_local_group():
    from app.services.system_config import SystemConfig

    assert not hasattr(SystemConfig(), "stt_local")
```

Edit `tests/unit/test_system_config_routes.py`:

```python
def test_get_config_includes_nested_groups_with_defaults(client):
    data = client.get("/v1/system/config").json()["data"]
    assert data["engines"]["default_stt_engine"] == "vosk"
    assert data["engines"]["stt_segment_min_seconds"] == 30.0
    assert data["conversation"]["conversation_silence_ms"] == 700
    assert data["preprocessing"]["stt_vad_backend"] == "energy"


def test_put_updates_a_nested_field_and_preserves_others(client):
    full = client.get("/v1/system/config").json()["data"]
    full["engines"]["default_stt_engine"] = "qwen3_asr"
    resp = client.put("/v1/system/config", json=full)
    data = resp.json()["data"]
    assert data["engines"]["default_stt_engine"] == "qwen3_asr"
    assert data["conversation"]["conversation_silence_ms"] == 700  # unrelated group untouched
```

Also check `test_malformed_field_type_returns_422_not_500` (same file, PUTs `{"engines": {"warmup_startup_timeout_s": "not-a-number"}}`) — `warmup_startup_timeout_s` was removed from `EngineDefaults` in Task 2, so this test is already stale from that task. Fix it now if it wasn't already:

```python
def test_malformed_field_type_returns_422_not_500(client):
    """Regression test: switching the route to manual request.json() +
    SystemConfig.model_validate() (to enable the deep-merge fix above) must not
    lose the structured 422 FastAPI gave for free when the param was a typed
    `payload: SystemConfig` -- a wrong-typed field should be a 422 JSON error,
    not a bare 500 text/plain response."""
    resp = client.put(
        "/v1/system/config",
        json={"engines": {"stt_segment_concurrency": "not-a-number"}},
    )
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["detail"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_system_config_store.py tests/unit/test_system_config_routes.py -v`
Expected: FAIL (fields/groups referenced don't match current code yet)

- [ ] **Step 3: Merge the fields and delete `SttLocalConfig`**

Edit `apps/api_gateway/app/services/system_config.py`:

```python
class EngineDefaults(BaseModel):
    default_stt_engine: str = "vosk"
    default_tts_engine: str = "omnivoice"
    default_tts_engine_voice: str = ""  # optional VieNeu preset voice
    # Long-audio segmentation: split a clip into chunks and transcribe them in
    # parallel. Batch /v1/stt/transcribe only -- the live conversation flow
    # never uses this (utterances are already short). Previously its own
    # top-level "STT (Shared Settings)" group; folded in here once the
    # group's other 4 fields moved to env (see Settings) -- 3 fields didn't
    # warrant a standalone accordion.
    stt_segment_long_enabled: bool = False
    stt_segment_min_seconds: float = 30.0
    stt_segment_concurrency: int = 4
```

Delete the entire `SttLocalConfig` class.

```python
class SystemConfig(BaseModel):
    base_context: str = ""
    engines: EngineDefaults = EngineDefaults()
    conversation: ConversationTuningConfig = ConversationTuningConfig()
    preprocessing: PreprocessingConfig = PreprocessingConfig()
```

(drops the `stt_local: SttLocalConfig = SttLocalConfig()` line)

- [ ] **Step 4: Update `stt.py`'s segmentation call site**

Edit `apps/api_gateway/app/api/routes/stt.py:69-82`:

```python
    # Long clips: split on silence and transcribe segments in parallel (higher
    # throughput). Only when enabled and the clip is at/over the length threshold.
    engines = system_config_store.get().engines
    use_segment = _resolve_flag(segment, engines.stt_segment_long_enabled)
    try:
        if use_segment and wav_duration_seconds(audio_bytes) >= engines.stt_segment_min_seconds:
            pcm, sample_rate, _, _ = read_wav(audio_bytes)
            result = await transcribe_long(
                provider,
                pcm16_to_float_array(pcm),
                sample_rate,
                language=payload.language,
                concurrency=engines.stt_segment_concurrency,
            )
```

(replaces `stt_local = system_config_store.get().stt_local` and every `stt_local.stt_segment_*` reference)

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_system_config_store.py tests/unit/test_system_config_routes.py -v`
Expected: all PASS

- [ ] **Step 6: Run the STT route test file**

Run: `pytest tests/unit/test_stt_routes.py -v` (or whatever the actual filename is — first run `find tests -iname "*stt*route*" -o -iname "*test_stt.py"` to confirm; the `/v1/stt/transcribe` segmentation behavior should have its own test file)
Expected: all PASS. If a segmentation test references `stt_local` directly (e.g. constructs a `SystemConfig` with a `stt_local=` kwarg), update it to use `engines=EngineDefaults(stt_segment_long_enabled=...)` instead, following the same pattern as this task's other edits.

- [ ] **Step 7: Commit**

```bash
git add apps/api_gateway/app/services/system_config.py apps/api_gateway/app/api/routes/stt.py tests/unit/test_system_config_store.py tests/unit/test_system_config_routes.py
git commit -m "$(cat <<'EOF'
refactor(config): merge stt_segment_* into EngineDefaults, delete stt_local group

The 3 remaining stt_local fields are read exclusively by the batch
/v1/stt/transcribe endpoint, never the conversation flow -- not "shared"
(the group's old name was misleading) and don't warrant a standalone
top-level Settings accordion on their own. Folds into a
"Long-audio segmentation (batch STT)" sub-block of Engine Defaults instead.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Remove `conversation_stt_engine`/`conversation_tts_engine` + query-param engine tier

**Files:**
- Modify: `apps/api_gateway/app/services/stt/profile.py` (`resolve_stt`)
- Modify: `apps/api_gateway/app/api/routes/conversation.py:221-247`
- Modify: `apps/api_gateway/app/api/routes/lugo.py:44-64`
- Modify: `apps/api_gateway/app/api/routes/livehost.py:104-131`
- Modify: `apps/api_gateway/app/services/system_config.py` (`ConversationTuningConfig`, `warmup_stt_engines`, `warmup_tts_engines`)
- Modify: `apps/api_gateway/app/main.py:43-51` (docstring, second edit)
- Modify: `apps/api_gateway/app/static/js/system-config.js:56,58` (`ENGINE_SELECT_FIELDS` — drop 2 entries; full file rewrite happens in Task 12, this is a minimal fix so the file stays internally consistent between now and then)
- Modify: `tests/unit/test_stt_profile.py`
- Modify: `tests/unit/test_system_config_store.py` (`test_conversation_tuning_config_has_expected_defaults`)
- Modify: `tests/unit/test_warmup_engine_settings.py` (source switch)
- Modify: `tests/unit/test_conversation_tts_profile.py`
- Modify: `tests/unit/test_livehost_tts_profile.py`
- Modify: `tests/integration/test_conversation_ws.py`
- Modify: `tests/integration/test_livehost_ws_voice.py`

**Interfaces:**
- Produces: `resolve_stt(profile, q_language=None, q_model=None) -> tuple[str, str|None, str]` (drops the `q_engine` parameter). `ConversationTuningConfig` down to 19 fields (drops `conversation_stt_engine`, `conversation_tts_engine`). Engine resolution everywhere is now `profile config > engines.default_stt_engine`/`default_tts_engine` — no query param, no conversation-wide override.

- [ ] **Step 1: Update `resolve_stt`'s test first**

Replace `tests/unit/test_stt_profile.py` in full:

```python
import pytest

from app.services.profiles.models import Profile, SttConfig
from app.services.stt.profile import resolve_stt
from app.services.system_config import SystemConfigStore


# --- resolve_stt: profile-driven STT resolution -----------------------------

@pytest.fixture
def _server_default(monkeypatch, tmp_path):
    # Pin the server-wide default so tests don't depend on the ambient config DB.
    fresh = SystemConfigStore(str(tmp_path / "system_config.json"))
    fresh.set(
        fresh.get().model_copy(
            update={
                "engines": fresh.get().engines.model_copy(update={"default_stt_engine": "whisper"}),
                "conversation": fresh.get().conversation.model_copy(
                    update={"conversation_language": "vi"}
                ),
            }
        )
    )
    monkeypatch.setattr("app.services.system_config.system_config_store", fresh)


def test_resolve_stt_no_profile_uses_server_default(_server_default):
    assert resolve_stt(None) == ("whisper", "vi", "")


def test_resolve_stt_profile_engine_and_language_win_over_server_default(_server_default):
    p = Profile(name="p", stt=SttConfig(engine="qwen3_asr", language="en"))
    assert resolve_stt(p) == ("qwen3_asr", "en", "")


def test_resolve_stt_profile_engine_only_keeps_server_language(_server_default):
    p = Profile(name="p", stt=SttConfig(engine="qwen3_asr"))
    assert resolve_stt(p) == ("qwen3_asr", "vi", "")


def test_resolve_stt_query_language_wins_over_profile(_server_default):
    p = Profile(name="p", stt=SttConfig(engine="qwen3_asr", language="vi"))
    assert resolve_stt(p, q_language="fr") == ("qwen3_asr", "fr", "")


def test_resolve_stt_engines_default_when_no_profile(monkeypatch, tmp_path):
    fresh = SystemConfigStore(str(tmp_path / "system_config.json"))
    fresh.set(
        fresh.get().model_copy(
            update={
                "engines": fresh.get().engines.model_copy(update={"default_stt_engine": "vosk"}),
                "conversation": fresh.get().conversation.model_copy(
                    update={"conversation_language": ""}
                ),
            }
        )
    )
    monkeypatch.setattr("app.services.system_config.system_config_store", fresh)
    # No profile -> engines.default_stt_engine; empty conversation_language -> None (auto-detect).
    assert resolve_stt(None) == ("vosk", None, "")


def test_resolve_stt_model_from_profile(_server_default):
    # No explicit language on the SttConfig, so language falls back to the
    # server default (conversation_language="vi" per _server_default).
    p = Profile(name="p", stt=SttConfig(engine="qwen3_asr", model="1.7b"))
    assert resolve_stt(p) == ("qwen3_asr", "vi", "1.7b")


def test_resolve_stt_model_query_param_wins_over_profile(_server_default):
    p = Profile(name="p", stt=SttConfig(engine="qwen3_asr", model="1.7b"))
    assert resolve_stt(p, q_model="0.6b") == ("qwen3_asr", "vi", "0.6b")


def test_resolve_stt_model_defaults_empty_when_unset(_server_default):
    assert resolve_stt(None) == ("whisper", "vi", "")
```

(renames `test_resolve_stt_query_param_wins_over_profile` → `test_resolve_stt_query_language_wins_over_profile` and drops its `q_engine` assertion; renames `test_resolve_stt_engines_default_when_conversation_engine_empty` → `test_resolve_stt_engines_default_when_no_profile` and drops the `conversation_stt_engine` override, asserting the plain `engines.default_stt_engine` fallback directly)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_stt_profile.py -v`
Expected: FAIL — `resolve_stt() takes from 1 to 4 positional arguments but ...` / `TypeError` on keyword args that don't exist yet, or assertions against the old 4-arg signature's behavior.

- [ ] **Step 3: Rewrite `resolve_stt`**

Replace `apps/api_gateway/app/services/stt/profile.py` in full:

```python
"""STT engine/language/model resolution for a conversation.

No preset layer, no per-request engine override: a profile names the engine,
language, and model variant directly, falling back to the server-wide
defaults. See docs/superpowers/specs/2026-07-23-system-settings-restructure-design.md
for why the query-param engine override was removed.
"""

from __future__ import annotations


def resolve_stt(
    profile: object | None,
    q_language: str | None = None,
    q_model: str | None = None,
) -> tuple[str, str | None, str]:
    """Resolve (engine, language|None, model) for a conversation.

    Single source of truth shared by the conversation WS stream and the /stt/warm
    endpoint so a device that only sends a profile id warms and streams against the
    same STT model. Priority, highest first:

      1. the chatllm profile's SttConfig (engine/language/model)
      2. the server-wide default (default_stt_engine / conversation_language);
         model has no server-wide default — "" means "whatever's currently active
         for the resolved engine".

    `profile` is a services.profiles Profile (or None); accessed duck-typed to avoid
    a circular import. language None means auto-detect.
    """
    from app.services.system_config import system_config_store

    stt_cfg = getattr(profile, "stt", None)
    conv_cfg = system_config_store.get().conversation
    engine = (
        (getattr(stt_cfg, "engine", "") or None)
        or system_config_store.get().engines.default_stt_engine
    )
    if q_language:
        language: str | None = q_language
    elif getattr(stt_cfg, "language", ""):
        language = stt_cfg.language
    else:
        language = conv_cfg.conversation_language or None
    model = q_model or (getattr(stt_cfg, "model", "") or "")
    return engine, language, model
```

- [ ] **Step 4: Update `conversation.py`'s call site + inline TTS resolution**

Edit `apps/api_gateway/app/api/routes/conversation.py:221-223`:

```python
    stt_engine, language, stt_model = resolve_stt(
        profile, q.get("language"), q.get("stt_model")
    )
```

Edit `apps/api_gateway/app/api/routes/conversation.py:237-247`:

```python
    else:
        tts_engine = system_config_store.get().engines.default_tts_engine
        tts_model = q.get("tts_model") or ""
        voice = q.get("voice") or None
        ref_audio_path = ref_text = tts_instruct = None
        tts_speed = tts_language = None
```

(drops the `conv_cfg = system_config_store.get().conversation` line — that name has no other use in this function)

- [ ] **Step 5: Update `livehost.py`'s call site + inline TTS resolution**

Edit `apps/api_gateway/app/api/routes/livehost.py:105-107`:

```python
    stt_engine, language, stt_model = resolve_stt(
        profile, q.get("language"), q.get("stt_model")
    )
```

Edit `apps/api_gateway/app/api/routes/livehost.py:121-131`:

```python
    else:
        tts_engine = system_config_store.get().engines.default_tts_engine
        tts_model = q.get("tts_model") or ""
        voice = q.get("voice") or None
        ref_audio_path = ref_text = tts_instruct = None
        tts_speed = tts_language = None
```

(drops the `conv_cfg = system_config_store.get().conversation` line **at this location only** — `livehost.py` has 2 other, unrelated `conv_cfg = system_config_store.get().conversation` assignments further down the file, one for `VadEndpointer` timing params and one for Opus pacing; leave those two completely untouched)

- [ ] **Step 6: Update `lugo.py`'s `_resolve()`**

Edit `apps/api_gateway/app/api/routes/lugo.py:44-64`:

```python
def _resolve(profile_name: str | None):
    """Resolve engines/tts params from a profile (server owns everything)."""
    profile = profile_store.get(profile_name) if profile_name else None
    # Resolve STT from the profile's SttConfig (engine/language or a language
    # preset), falling back to server defaults — same single source of truth the
    # conversation stream uses, so a device that sends only a profile id streams
    # against that profile's STT. No query params on the Lugo wire.
    stt_engine, language, stt_model = resolve_stt(profile)
    tts_name = (profile.tts.profile_name if profile else "") or None
    tts_profile = tts_profile_store.get(tts_name) if tts_name else None
    if tts_profile and tts_profile.engine:
        tts = dict(engine=tts_profile.engine, model_id=tts_profile.model_id or "", voice=tts_profile.voice or None,
                   ref_audio_path=tts_profile.ref_audio_path or None, ref_text=tts_profile.ref_text or None,
                   instruct=tts_profile.instruct or None, speed=tts_profile.speed, language=tts_profile.language)
    else:
        tts = dict(
            engine=system_config_store.get().engines.default_tts_engine,
            model_id="", voice=None, ref_audio_path=None, ref_text=None, instruct=None, speed=None, language=None)
    idle = profile.session.idle_timeout_s if profile else 30
    return profile, stt_engine, language, stt_model, tts, idle
```

(the `resolve_stt(profile)` call itself is unchanged — it never passed query params — only the `conv_cfg`/`conversation_tts_engine` line changes)

- [ ] **Step 7: Remove the 2 fields from `ConversationTuningConfig`; switch the warm-up source**

Edit `apps/api_gateway/app/services/system_config.py`:

```python
class ConversationTuningConfig(BaseModel):
    conversation_silence_ms: int = 700
    conversation_min_silence_ms: int = 450
    conversation_adaptive_full_ms: int = 3000
    conversation_min_speech_ms: int = 300
    conversation_rms_threshold: float = 0.015
    conversation_preroll_ms: int = 600
    # Ignore a barge-in for this long after the assistant STARTS speaking. The
    # first frames the mic hears when the assistant begins are usually the
    # assistant's own audio echoed back (no/imperfect echo cancellation), which
    # would otherwise abort the turn instantly. 0 disables the grace (barge-in
    # from the first frame). Clients that half-duplex their mic never hit this.
    conversation_barge_in_grace_ms: int = 500
    conversation_max_utterance_ms: int = 30000
    conversation_goodbye_text: str = "Hẹn gặp lại nha!"
    conversation_fast_stt_engine: str = ""
    conversation_fast_stt_max_ms: int = 1500
    conversation_streaming_stt: bool = False
    conversation_streaming_chunk_ms: int = 1000
    conversation_tts_lookahead: int = 3
    conversation_opus_pace: bool = False
    conversation_opus_prebuffer_frames: int = 5
    conversation_language: str = "vi"
    # Shared HTTP timeout for every OpenAI-compatible LLM call (chat responder,
    # memory extraction/compaction, embeddings) -- not tied to one Model
    # Registry entry since some of these calls target a per-profile LLM, not
    # "the" conversation LLM.
    llm_timeout_seconds: float = 60.0
    conversation_system_prompt: str = (
        "You are a helpful, concise voice assistant. Reply in the user's language, "
        "in 2-4 short sentences suitable for being spoken aloud. "
        "Your reply is read aloud by text-to-speech, so write plain speakable prose only: "
        "do NOT use emojis, emoticons, kaomoji, or decorative/pictographic symbols, "
        "and avoid markdown, bullet points, or code blocks. "
        "Write in complete, flowing sentences ending with a normal period. "
        "Do NOT use ellipses (…) or trailing dots for dramatic pauses, and do NOT put "
        "line breaks inside a thought or split dialogue across multiple lines."
    )
```

(drops `conversation_stt_engine: str = "whisper"` and `conversation_tts_engine: str = "omnivoice"`)

```python
def warmup_stt_engines() -> list[str]:
    engine = system_config_store.get().engines.default_stt_engine
    return [engine] if engine else []


def warmup_tts_engines() -> list[str]:
    engine = system_config_store.get().engines.default_tts_engine
    return [engine] if engine else []
```

- [ ] **Step 8: Update `main.py`'s docstring (second edit)**

Edit `apps/api_gateway/app/main.py:43-51`:

```python
async def _warm_default_engines() -> None:
    """Load the STT/TTS engines conversations actually use, at process boot instead
    of waiting for the first WebSocket connect. Covers default_stt_engine /
    default_tts_engine, plus every engine any chatllm/TTS profile can select (see
    app.services.warmup.engines_for_boot_warmup) -- so a device connecting with
    any profile never pays a cold model load on its first turn. Engine selection
    is profile-or-default only (no per-request override), so boot warm-up can see
    every engine a device might ever request."""
```

- [ ] **Step 9: Fix `system-config.js`'s `ENGINE_SELECT_FIELDS` so the page doesn't break before Task 12's full rewrite**

Edit `apps/api_gateway/app/static/js/system-config.js:53-59`:

```javascript
const ENGINE_SELECT_FIELDS = {
  "engines.default_stt_engine": { kind: "stt" },
  "engines.default_tts_engine": { kind: "tts" },
  "conversation.conversation_fast_stt_engine": { kind: "stt", optional: true },
};
```

(drops the `conversation.conversation_stt_engine` and `conversation.conversation_tts_engine` entries — this is a minimal patch so the JS doesn't reference dead field names between now and Task 12's full rewrite; the `GROUPS`/`stt_local` label etc. are still stale here and get fixed in Task 12)

- [ ] **Step 10: Update `test_system_config_store.py`'s conversation-defaults test**

Edit `tests/unit/test_system_config_store.py`:

```python
def test_conversation_tuning_config_has_expected_defaults(tmp_path):
    s = SystemConfigStore(str(tmp_path / "system_config.json"))
    c = s.get().conversation
    assert c.conversation_silence_ms == 700
    assert c.conversation_min_silence_ms == 450
    assert c.conversation_adaptive_full_ms == 3000
    assert c.conversation_min_speech_ms == 300
    assert c.conversation_rms_threshold == 0.015
    assert c.conversation_preroll_ms == 600
    assert c.conversation_max_utterance_ms == 30000
    assert c.conversation_goodbye_text == "Hẹn gặp lại nha!"
    assert not hasattr(c, "conversation_stt_engine")
    assert not hasattr(c, "conversation_tts_engine")
    assert c.conversation_fast_stt_engine == ""
    assert c.conversation_fast_stt_max_ms == 1500
    assert c.conversation_streaming_stt is False
    assert c.conversation_streaming_chunk_ms == 1000
    assert c.conversation_tts_lookahead == 3
    assert c.conversation_opus_pace is False
    assert c.conversation_opus_prebuffer_frames == 5
    assert c.conversation_language == "vi"
    assert c.llm_timeout_seconds == 60.0
    assert "helpful, concise voice assistant" in c.conversation_system_prompt
```

- [ ] **Step 11: Update `test_warmup_engine_settings.py`'s source**

Edit `tests/unit/test_warmup_engine_settings.py`, replace `_patch_conversation_engines` (added in Task 5) with a version that sets `engines.default_stt_engine`/`default_tts_engine` instead:

```python
def _patch_default_engines(monkeypatch, tmp_path, *, stt_engine="whisper", tts_engine="vieneu"):
    fresh = SystemConfigStore(str(tmp_path / "system_config.json"))
    fresh.set(
        fresh.get().model_copy(
            update={
                "engines": fresh.get().engines.model_copy(
                    update={"default_stt_engine": stt_engine, "default_tts_engine": tts_engine}
                ),
            }
        )
    )
    monkeypatch.setattr(sc_mod, "system_config_store", fresh)


def test_warmup_stt_engines_returns_the_default_engine(monkeypatch, tmp_path):
    _patch_default_engines(monkeypatch, tmp_path, stt_engine="whisper")
    assert sc_mod.warmup_stt_engines() == ["whisper"]


def test_warmup_tts_engines_returns_the_default_engine(monkeypatch, tmp_path):
    _patch_default_engines(monkeypatch, tmp_path, tts_engine="vieneu")
    assert sc_mod.warmup_tts_engines() == ["vieneu"]
```

Rename every remaining `_patch_conversation_engines(...)` call further down the file (in `test_boot_warmup_includes_profile_and_tts_profile_engines`/`test_boot_warmup_collects_profile_stt_models`) to `_patch_default_engines(...)`.

- [ ] **Step 12: Update the profile-driven TTS tests to stop using query-param TTS override**

Edit `tests/unit/test_conversation_tts_profile.py`:

Add `default_stt_engine`/`default_tts_engine` monkeypatching to `_local_hermetic` so tests don't need `stt_engine=`/`tts_engine=` query params any more:

```python
@pytest.fixture(autouse=True)
def _local_hermetic(monkeypatch, tmp_path):
    # Named distinctly from conftest.py's `_hermetic` so both autouse fixtures
    # run (a same-named fixture here would shadow, not compose with, the
    # global one).
    stt_service.providers["stub-conv-ttsp"] = _StubSTT()
    stub_tts = _RecordingTTS()
    tts_service.providers["stub-conv-ttsp-tts"] = stub_tts

    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    fresh_tts_profiles = TtsProfileStore(str(tmp_path / "tts_profiles.json"))
    monkeypatch.setattr("app.api.routes.conversation.profile_store", fresh_profiles)
    monkeypatch.setattr("app.api.routes.conversation.tts_profile_store", fresh_tts_profiles)

    from app.services import system_config as sc_mod

    fresh_config = sc_mod.SystemConfigStore(str(tmp_path / "system_config.json"))
    fresh_config.set(
        fresh_config.get().model_copy(
            update={
                "engines": fresh_config.get().engines.model_copy(
                    update={"default_stt_engine": "stub-conv-ttsp", "default_tts_engine": "stub-conv-ttsp-tts"}
                ),
            }
        )
    )
    monkeypatch.setattr("app.api.routes.conversation.system_config_store", fresh_config)
    monkeypatch.setattr(sc_mod, "system_config_store", fresh_config)

    yield stub_tts, fresh_profiles, fresh_tts_profiles

    stt_service.providers.pop("stub-conv-ttsp", None)
    tts_service.providers.pop("stub-conv-ttsp-tts", None)
```

(patches **both** `app.api.routes.conversation.system_config_store` — the name bound in that module at its own top-level import — **and** `app.services.system_config.system_config_store`, since `resolve_stt` re-imports the latter locally on every call but `conversation.py`'s own `system_config_store.get().engines.default_tts_engine` reference resolves against the name bound in its own module namespace; patching only one would leave the other route reading the real, un-monkeypatched singleton)

Then remove `stt_engine=stub-conv-ttsp` (and `&tts_engine=stub-conv-ttsp-tts` where present) from every URL in the 4 test functions:

```python
    url = "/v1/conversation/stream?profile=host&sample_rate=16000"
```

```python
    url = (
        "/v1/conversation/stream?profile=host"
        "&tts_profile=pinned&sample_rate=16000"
    )
```

```python
    url = "/v1/conversation/stream?profile=device&sample_rate=16000"
```

Rewrite `test_no_tts_profile_falls_back_to_legacy_query_params` (the whole premise — a `tts_engine=` query param determining the TTS engine — no longer applies):

```python
def test_no_tts_profile_falls_back_to_default_tts_engine(client, _local_hermetic):
    stub_tts, _profiles, _tts_profiles = _local_hermetic
    url = "/v1/conversation/stream?voice=manual-voice&sample_rate=16000"
    with client.websocket_connect(url) as ws:
        assert ws.receive_json()["event"] == "session_started"
        _run_one_turn(ws)

    payload = stub_tts.calls[0]
    assert payload.engine == "stub-conv-ttsp-tts"
    assert payload.voice == "manual-voice"
    assert payload.ref_audio_path is None
    assert payload.instruct is None
    assert payload.speed is None
```

(`voice=manual-voice` stays — `voice` is a distinct, unaffected query param, not part of the engine-selection tier being removed)

Apply the identical set of changes to `tests/unit/test_livehost_tts_profile.py` (same fixture pattern, patch `app.api.routes.livehost.system_config_store` instead of `app.api.routes.conversation.system_config_store`; same 4-test URL cleanup; rename `test_livehost_no_tts_profile_falls_back_to_legacy_query_params` → `test_livehost_no_tts_profile_falls_back_to_default_tts_engine` with the same rewrite).

- [ ] **Step 13: Update the WS integration tests to select engines via profile instead of query param**

Edit `tests/integration/test_conversation_ws.py`, add a profile-store fixture and a helper, and switch every test to connect via `?profile=`:

```python
import asyncio

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.stt import STTResult
from app.schemas.tts import TTSResult
from app.services.profiles.models import Profile, SttConfig
from app.services.profiles.store import ProfileStore
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service

SR = 16000


class _StubSTT(STTProvider):
    name = "stub-conv"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chào trợ lý", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-conv-tts"

    async def synthesize(self, payload) -> TTSResult:
        return TTSResult(
            engine=self.name, sample_rate=24000, audio_url="/artifacts/x.wav",
            duration_seconds=0.1, text=payload.text,
        )


class _SlowTTS(TTSProvider):
    name = "slow-conv-tts"

    async def synthesize(self, payload) -> TTSResult:
        await asyncio.sleep(0.5)  # window for barge-in
        return TTSResult(
            engine=self.name, sample_rate=24000, audio_url="/artifacts/x.wav",
            duration_seconds=0.1, text=payload.text,
        )


@pytest.fixture(autouse=True)
def _register_stub(monkeypatch, tmp_path):
    # Keep the test hermetic regardless of .env: stub TTS + built-in echo responder
    # (no external Ollama / real model calls). conversation_llm_base_url now
    # lives on system_config_store; conftest._hermetic already zeroes it.
    stt_service.providers["stub-conv"] = _StubSTT()
    tts_service.providers["stub-conv-tts"] = _StubTTS()
    tts_service.providers["slow-conv-tts"] = _SlowTTS()

    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    monkeypatch.setattr("app.api.routes.conversation.profile_store", fresh_profiles)

    yield fresh_profiles

    stt_service.providers.pop("stub-conv", None)
    tts_service.providers.pop("stub-conv-tts", None)
    tts_service.providers.pop("slow-conv-tts", None)


def _loud(ms: int) -> bytes:
    n = int(SR * ms / 1000)
    return (np.full(n, 0.2, dtype=np.float32) * 32767).astype("<i2").tobytes()


def _silence(ms: int) -> bytes:
    return (b"\x00\x00") * int(SR * ms / 1000)


def _next_event(ws) -> dict:
    """Read the next event, transparently skipping "engines_ready" — it fires
    asynchronously whenever the engine finishes cold-loading and can land at any
    point in the stream, same as a real client (which handles it by name, not
    by position) would treat it."""
    while True:
        ev = ws.receive_json()
        if ev["event"] != "engines_ready":
            return ev


def test_conversation_turn_end_to_end(_register_stub):
    _register_stub.upsert(Profile(name="p1", stt=SttConfig(engine="stub-conv")))
    client = TestClient(app)
    url = "/v1/conversation/stream?profile=p1&tts_engine=stub-conv-tts&sample_rate=16000"
```

Wait — `tts_engine=` is also being removed; the TTS engine in this test needs a `tts_profile=` instead (a `TtsProfile`), OR since `conversation.py` monkeypatches `default_tts_engine`, use that. Given this file currently has no `tts_profile_store`/`TtsProfileStore` fixture at all, the simplest fix consistent with the rest of this task is to monkeypatch `engines.default_tts_engine` once for the whole module (all 3 WS tests in this file use `stub-conv-tts` except the barge-in test, which uses `slow-conv-tts` — so `default_tts_engine` needs to vary per test, not be fixed module-wide). Use a small per-test helper instead:

```python
def _set_default_tts(monkeypatch, tmp_path, tts_engine):
    from app.services import system_config as sc_mod

    fresh = sc_mod.SystemConfigStore(str(tmp_path / "system_config.json"))
    fresh.set(
        fresh.get().model_copy(
            update={"engines": fresh.get().engines.model_copy(update={"default_tts_engine": tts_engine})}
        )
    )
    monkeypatch.setattr("app.api.routes.conversation.system_config_store", fresh)
    monkeypatch.setattr(sc_mod, "system_config_store", fresh)


def test_conversation_turn_end_to_end(_register_stub, monkeypatch, tmp_path):
    _register_stub.upsert(Profile(name="p1", stt=SttConfig(engine="stub-conv")))
    _set_default_tts(monkeypatch, tmp_path, "stub-conv-tts")
    client = TestClient(app)
    url = "/v1/conversation/stream?profile=p1&sample_rate=16000"
    with client.websocket_connect(url) as ws:
        assert ws.receive_json()["event"] == "session_started"

        ws.send_bytes(_loud(500))
        assert _next_event(ws)["event"] == "speech_start"
        ws.send_bytes(_loud(400))
        ws.send_bytes(_silence(500))
        ws.send_bytes(_silence(500))  # crosses 700ms silence -> endpoint

        events = []
        for _ in range(30):
            ev = ws.receive_json()
            if ev["event"] == "engines_ready":
                continue
            events.append(ev)
            if ev["event"] == "turn_done":
                break

        types = [e["event"] for e in events]
        assert "speech_end" in types
        assert "user_transcript" in types
        assert "response_text" in types
        assert "audio_chunk" in types
        assert types[-1] == "turn_done"

        transcript = next(e for e in events if e["event"] == "user_transcript")
        assert transcript["text"] == "xin chào trợ lý"
        chunk = next(e for e in events if e["event"] == "audio_chunk")
        assert chunk["audio_url"].startswith("/artifacts/")


def test_conversation_barge_in_aborts_turn(_register_stub, monkeypatch, tmp_path):
    _register_stub.upsert(Profile(name="p2", stt=SttConfig(engine="stub-conv")))
    _set_default_tts(monkeypatch, tmp_path, "slow-conv-tts")
    client = TestClient(app)
    url = "/v1/conversation/stream?profile=p2&sample_rate=16000"
    with client.websocket_connect(url) as ws:
        assert ws.receive_json()["event"] == "session_started"
        ws.send_bytes(_loud(500))
        assert _next_event(ws)["event"] == "speech_start"
        ws.send_bytes(_loud(400))
        ws.send_bytes(_silence(500))
        ws.send_bytes(_silence(500))  # endpoint -> turn starts (slow TTS)
        ws.send_bytes(_loud(500))  # barge-in while assistant is synthesizing

        seen = []
        for _ in range(12):
            ev = ws.receive_json()["event"]
            if ev == "engines_ready":
                continue
            seen.append(ev)
            if ev == "aborted":
                break
        assert "aborted" in seen  # the in-progress turn was cancelled


def test_conversation_unknown_engine_errors(_register_stub):
    _register_stub.upsert(Profile(name="p3", stt=SttConfig(engine="nope")))
    client = TestClient(app)
    with client.websocket_connect("/v1/conversation/stream?profile=p3") as ws:
        assert ws.receive_json()["event"] == "error"


def test_conversation_llm_config_set_and_reset():
    # No manual cleanup needed: the LLM config now lives in a Model Registry
    # DB row, and conftest's per-test tmp DB already isolates this from other
    # tests (unlike the old in-memory globals, which needed an explicit reset).
    client = TestClient(app)
    # Default (hermetic): no base url -> echo responder.
    body = client.get("/v1/conversation/llm").json()["data"]
    assert body["responder"] == "echo"

    # Point at an online OpenAI-compatible endpoint.
    body = client.post(
        "/v1/conversation/llm",
        json={"base_url": "https://api.example.com/v1", "api_key": "sk-x", "model": "gpt-test"},
    ).json()["data"]
    assert body["responder"] == "llm"
    assert body["base_url"] == "https://api.example.com/v1"
    assert body["model"] == "gpt-test"
    assert body["api_key_set"] is True  # key is never echoed back, only a flag

    # Revert.
    body = client.post("/v1/conversation/llm/reset").json()["data"]
    assert body["responder"] == "echo"
```

(`test_conversation_llm_config_set_and_reset` is untouched — it never used `stt_engine`)

Apply the same transformation shape to `tests/integration/test_livehost_ws_voice.py`: add a `ProfileStore` fixture patched onto `app.api.routes.livehost.profile_store`, a `_set_default_tts(monkeypatch, tmp_path, tts_engine)` helper patching `app.api.routes.livehost.system_config_store` + `app.services.system_config.system_config_store`, and for each of the 4 tests that currently pin `stt_engine=`/`tts_engine=` in the URL: create a `Profile(name="pN", stt=SttConfig(engine="<the stub>"))`, call `_set_default_tts(..., "<the stub tts>")`, and connect with `?profile=pN&...` (keeping every other existing query param — `sample_rate`, etc. — unchanged).

- [ ] **Step 14: Run all affected test files**

Run: `pytest tests/unit/test_stt_profile.py tests/unit/test_system_config_store.py tests/unit/test_warmup_engine_settings.py tests/unit/test_conversation_tts_profile.py tests/unit/test_livehost_tts_profile.py tests/integration/test_conversation_ws.py tests/integration/test_livehost_ws_voice.py -v`
Expected: all PASS

- [ ] **Step 15: Commit**

```bash
git add apps/api_gateway/app/services/stt/profile.py apps/api_gateway/app/api/routes/conversation.py apps/api_gateway/app/api/routes/lugo.py apps/api_gateway/app/api/routes/livehost.py apps/api_gateway/app/services/system_config.py apps/api_gateway/app/main.py apps/api_gateway/app/static/js/system-config.js tests/unit/test_stt_profile.py tests/unit/test_system_config_store.py tests/unit/test_warmup_engine_settings.py tests/unit/test_conversation_tts_profile.py tests/unit/test_livehost_tts_profile.py tests/integration/test_conversation_ws.py tests/integration/test_livehost_ws_voice.py
git commit -m "$(cat <<'EOF'
refactor(conversation): drop conversation-level + query-param STT/TTS engine override

Engine resolution is now profile config > server default, full stop --
no system-wide conversation_stt_engine/conversation_tts_engine tier and
no per-request ?stt_engine=/?tts_engine= override. Both were confusing,
overlapping knobs; a profile-level override already covers genuine
per-device/per-persona customization. Confirmed, intentional behavior
change to the device-connect protocol (see design spec) -- doc updates
and the now-dead Chat/Livehost engine-select dropdowns follow in
subsequent commits.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Remove the STT engine dropdown from the Chat page

**Files:**
- Modify: `apps/api_gateway/app/static/index.html:277`
- Modify: `apps/api_gateway/app/static/js/conversation.js` (lines 187, 202, 221, 223, 252, 256, 264, 268, 297, 304-305, 309, 311, 322 per grep — read the file fresh before editing since line numbers shift as you go)

**Interfaces:**
- Produces: no `#conv-stt-engine` element; `startConversation()` no longer sends `stt_engine=` on WS connect; `/v1/stt/warm` is always called with `profile=` (or no params) instead of `engine=`.

- [ ] **Step 1: Read the current file and locate every reference**

Run: `grep -n "conv-stt-engine" apps/api_gateway/app/static/index.html apps/api_gateway/app/static/js/conversation.js`

- [ ] **Step 2: Remove the `<select>` from `index.html`**

Read the surrounding markup at `index.html:270-285` first (it's inside a labeled form field — likely `<label>STT engine<select id="conv-stt-engine"></select></label>` or similar), then delete that whole label/select block. Use the Read tool to view lines 260-290 before editing, since the exact wrapper markup wasn't captured during planning.

- [ ] **Step 3: Remove the dropdown's JS logic from `conversation.js`**

Read `apps/api_gateway/app/static/js/conversation.js` in full first (it's ~350+ lines; the grep above gives every line number to inspect). Apply these changes:

- Delete the `fill("conv-stt-engine", stt.data.filter((e) => e.available), (e) => \`${e.engine}\`)` call (line ~252) and the `restoreAndBind("conv-stt-engine")` call (line ~264) and the `el("conv-stt-engine").addEventListener("change", updateConvEnginesInfo)` line (line ~268) — these populate/restore/wire the now-deleted dropdown.
- Delete the `const sttSel = el("conv-stt-engine")` declarations (lines ~202, ~221) and whatever logic reads `sttSel` immediately after each — read the surrounding function bodies first to see exactly what they do (likely toggling an "engine info" panel) and remove only the dead branch that depended on the dropdown's value, keeping any sibling logic (e.g. profile-based engine info) intact.
- Delete `const sttEng = el("conv-stt-engine")?.value || ""` (line ~187) and whatever it feeds into — read the surrounding function first.
- Delete `savePref("conv-stt-engine", "")` (line ~223) — the dropdown's localStorage preference no longer applies.
- In `startConversation()` (around line 297-322): delete `const sttEngine = el("conv-stt-engine").value;`, and simplify:

```javascript
  // Warm up the STT engine so the first turn doesn't stall loading the model.
  // The /warm endpoint is a fast no-op for engines with no warm() method.
  setConvStatus("⏳ starting STT engine…", "status-idle");
  try {
    const warmParams = activeProfile ? `profile=${encodeURIComponent(activeProfile)}` : "";
    const warmRes = await fetch(`/v1/stt/warm?${warmParams}`, { method: "POST" });
    if (!warmRes.ok) {
      setConvStatus("STT engine (server default) not ready", "status-error");
      setConvUI("idle");
      return;
    }
  } catch {
    setConvStatus("Could not connect to STT engine", "status-error");
    setConvUI("idle");
    return;
  }

  let params = `sample_rate=${STREAM_SAMPLE_RATE}`;
  if (activeProfile) params += `&profile=${encodeURIComponent(activeProfile)}`;
```

(drops the `sttEngine` variable entirely, the `warmParams` ternary's `engine=` branch, the `if (sttEngine) params += ...` line, and the `sttEngine || "(profile default)"` fallback text in the error message)

- [ ] **Step 4: Manual browser check**

Start the dev server (check `README.md`/`CLAUDE.md` for the run command — likely `uvicorn app.main:app --reload` from `apps/api_gateway`), open the admin console, go to Chat, confirm: no STT-engine dropdown is visible, starting a conversation still works end-to-end (mic → transcript → reply), and the browser console shows no reference errors to `conv-stt-engine`.

- [ ] **Step 5: Run the frontend test suite if one exists**

Run: `find . -iname "*.test.js" -path "*conversation*" 2>/dev/null` to check; if a JS test file references `conv-stt-engine`, update it the same way. If no JS test suite covers this page, skip.

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/static/index.html apps/api_gateway/app/static/js/conversation.js
git commit -m "$(cat <<'EOF'
refactor(ui): remove non-functional STT engine dropdown from Chat page

The dropdown sent ?stt_engine= on WS connect, a tier removed from
engine resolution in the previous commit -- leaving it in place would
silently do nothing, which is worse than not having it.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Remove the STT engine dropdown from the Livehost page

**Files:**
- Modify: `apps/api_gateway/app/static/index.html:386`
- Modify: `apps/api_gateway/app/static/js/livehost.js` (lines 16, 18, 206, 224, 249, 254-255, 259, 261, 275 per grep — same caveat as Task 8, re-grep and re-read before editing)

**Interfaces:**
- Produces: no `#lh-stt-engine` element; Livehost session start no longer sends `stt_engine=` on WS connect.

- [ ] **Step 1: Read the current file and locate every reference**

Run: `grep -n "lh-stt-engine" apps/api_gateway/app/static/index.html apps/api_gateway/app/static/js/livehost.js`

- [ ] **Step 2: Remove the `<select>` from `index.html`**

Read lines 380-395 first, then delete the label/select block wrapping `id="lh-stt-engine"`, mirroring Task 8 Step 2.

- [ ] **Step 3: Remove the dropdown's JS logic from `livehost.js`**

Read the file in full first. Apply the same shape of changes as Task 8 Step 3: delete the population/restore/bind calls (lines ~16, 18, 206, 224), and in the session-start function (~line 249-275):

```javascript
  setLhStatus("⏳ starting STT engine…", "status-idle");
  try {
    const warmParams = profile ? `profile=${encodeURIComponent(profile)}` : "";
    const warmRes = await fetch(`/v1/stt/warm?${warmParams}`, { method: "POST" });
    if (!warmRes.ok) {
      setLhStatus("STT engine (server default) not ready", "status-error");
      setLhSessionUI("idle");
      return;
    }
  } catch {
    setLhStatus("Could not connect to STT engine", "status-error");
    setLhSessionUI("idle");
    return;
  }

  const soloMode = !!el("lh-mode-solo")?.checked;

  lh.sessionId = crypto.randomUUID();
  let params = `session_id=${encodeURIComponent(lh.sessionId)}`;
  params += `&sample_rate=${STREAM_SAMPLE_RATE}`;
  if (profile) params += `&profile=${encodeURIComponent(profile)}`;
```

(drops `const sttEngine = el("lh-stt-engine").value;` and the `if (sttEngine) params += ...` line and the `sttEngine || "(profile default)"` error-message fallback)

- [ ] **Step 4: Manual browser check**

Open the admin console's Livehost page, confirm no STT-engine dropdown, and that starting a solo/co-host session still works.

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/static/index.html apps/api_gateway/app/static/js/livehost.js
git commit -m "$(cat <<'EOF'
refactor(ui): remove non-functional STT engine dropdown from Livehost page

Same reasoning as the Chat page dropdown removal in the previous commit.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Update device-integration docs

**Files:**
- Modify: `docs/api.md`
- Modify: `docs/device-integration.md`
- Modify: `rpi-assistant/integration.md`
- Modify: `esp32-assistant/README.md`

**Interfaces:**
- Produces: no doc references `?stt_engine=`/`?tts_engine=` as a device-connect param; the `GET /v1/system/config` JSON example in `docs/api.md` matches the final field shape from Tasks 1–7.

- [ ] **Step 1: Update `docs/api.md`'s query-param table and prose**

Edit `docs/api.md`:

```
### `WS /v1/conversation/stream`
A unified **text/audio → text/audio** gateway (browser + IoT). Input is either audio
frames (VAD-endpointed) or a text message; output is text events and/or synthesized
audio. Supports the full matrix: audio→audio, text→audio, audio→text, text→text.

```
ws://localhost:8000/v1/conversation/stream?profile=vi&sample_rate=16000&audio_codec=opus&output=audio,text&audio_out=opus&output_sample_rate=24000
```

| query param | default | meaning |
|-------------|---------|---------|
| `voice` / `language` | settings | per-session voice/language (engine selection is profile-or-server-default only, see below) |
| `profile` | — | named **chatllm profile** (see below) — sets LLM model/system prompt/TTS/MCP tools/memory in one shot |
| `sample_rate` | 16000 | input audio rate (Hz) |
| `audio_codec` | `pcm16` | **input** codec: `pcm16` or `opus` |
| `output` | `audio,text` | what to send back: any of `audio`, `text` |
| `audio_out` | `url` | reply-audio delivery: `url` (browser fetches /artifacts) or `opus` (binary frames pushed — for devices) |
| `output_sample_rate` | 24000 | output Opus frame rate when `audio_out=opus` |

**`profile`** does double duty:
1. If it names a saved profile (`POST /v1/profiles`), the session uses that profile's
   `stt.engine`/`language`, `llm` (base_url/api_key/model), `system_prompt`,
   `tts.engine`/`tts.voice`, `mcp_servers`, and `memory` settings — overriding server
   defaults. There is no per-request engine override query param — STT/TTS engine
   selection is always profile config, else the server-wide `default_stt_engine`/
   `default_tts_engine` (see `GET /v1/system/config`'s `engines` group).
2. If it matches a built-in **language preset** (`vi` / `en` / `multi` / `en_vi`), it also
   selects the STT engine + language for that language, unless `language` is passed
   explicitly. A profile can be named e.g. `vi` to get both behaviors at once.

If `profile` is set but unknown, the server replies with a `warning` event and falls back
to defaults (the connection still proceeds).
```

- [ ] **Step 2: Update the `GET /v1/system/config` JSON example and the "key changes" note**

Edit `docs/api.md:401-452`:

```
### `GET /v1/system/config`
Fetch the system configuration (preprocessing, conversation tuning, engine defaults).

Response `data`:
```json
{
  "base_context": "...",
  "engines": {
    "default_stt_engine": "vosk",
    "default_tts_engine": "omnivoice",
    "default_tts_engine_voice": "",
    "stt_segment_long_enabled": false,
    "stt_segment_min_seconds": 30.0,
    "stt_segment_concurrency": 4
  },
  "conversation": { ... },
  "preprocessing": { ... }
}
```

Key changes from earlier API versions:
- **Remote STT config** (`whisper_service`, `eventlab`) is no longer stored in SystemConfig.
  Configure remote STT engines via `POST /v1/model_registry` with `kind="stt"` entries (see below).
- **OmniVoice TTS config** is no longer stored in SystemConfig. Configure OmniVoice via
  `POST /v1/model_registry` with `kind="tts"` entries and store engine-specific settings in the
  `config` dict.
- **stt_local per-engine fields** have been removed — device/compute_type first, then the
  default model / model path and whisper decode tuning (`vosk_model_path`,
  `whisper_local_model`, `whisper_vad_filter`, `whisper_beam_size`,
  `whisper_condition_on_previous_text`, `whisper_initial_prompt`,
  `whisper_mlx_model_path`, `qwen3_asr_model`). Configure them per engine via the
  Model Registry `model_id=""` sentinel entries (`kind="stt"`, `engine="whisper_local"` /
  `"whisper_mlx"` / `"qwen3_asr"` / `"vosk"`), stored in the `config` dict — e.g.
  `{"default_model": "large-v3-turbo", "vad_filter": true, "beam_size": 1,
  "condition_on_previous_text": false, "initial_prompt": "", "device": "cpu",
  "compute_type": "int8"}` for `whisper_local`, `{"model_path": "..."}` for
  `vosk`/`whisper_mlx`.
- **The `stt_local` group is gone entirely.** Its 3 engine-agnostic long-audio
  segmentation fields (`stt_segment_long_enabled`, `stt_segment_min_seconds`,
  `stt_segment_concurrency`) moved into `engines`, above. Its 4 remaining fields
  (`stt_model_dir`, `vosk_model_base_url`, `stt_stream_sample_rate`,
  `stt_glossary_path`) are deployment-time constants now — set via env vars
  (`STT_MODEL_DIR`, `VOSK_MODEL_BASE_URL`, `STT_STREAM_SAMPLE_RATE`,
  `STT_GLOSSARY_PATH`), not exposed via this endpoint.
- **`preprocessing.pyannote_vad_model`/`pyannote_auth_token` are gone too** — same
  reasoning, now `PYANNOTE_VAD_MODEL`/`PYANNOTE_AUTH_TOKEN` env vars.
- **No per-request `?stt_engine=`/`?tts_engine=` query param** on `/v1/conversation/stream`,
  `/v1/livehost/stream`, or the Lugo protocol — engine selection is profile config, else
  `engines.default_stt_engine`/`default_tts_engine`.
```

(the exact original wording of the "Key changes" bullets before this edit should be re-read via `Read docs/api.md` before applying, since this plan reproduces it from an earlier read and the file may have shifted slightly by the time this task runs — treat the block above as the target end-state prose, adjust surrounding punctuation/lead-in to match if line numbers drifted)

- [ ] **Step 3: Update `docs/device-integration.md`**

Edit the query-param table (lines 20-32ish) to drop the `stt_engine`/`tts_engine` rows, and the example URLs:

```
| param | value | meaning |
|-------|-------|---------|
| `language` | `vi` | STT language hint |
| `sample_rate` | `16000` | **uplink** audio rate (Hz) |
| `audio_codec` | `opus` | uplink codec — raw Opus packets |
| `output` | `audio,text` | what to receive: `audio` (+ `text` for subtitles/debug) |
| `audio_out` | `opus` | reply audio delivered as **pushed Opus frames** (not a URL) |
| `output_sample_rate` | `24000` | **downlink** Opus rate (Hz) |
| `profile` | *(recommended)* | named **chatllm profile** — see §1a below. Engine selection has no per-request override any more; without a profile the device gets the server-wide `default_stt_engine`/`default_tts_engine`. |

Full example (server defaults for STT/TTS engine):
```
ws://192.168.1.50:8000/v1/conversation/stream?language=vi&sample_rate=16000&audio_codec=opus&output=audio,text&audio_out=opus&output_sample_rate=24000
```

Full example with a profile (recommended — pins STT/TTS engine explicitly instead of relying on the server default):
```
ws://192.168.1.50:8000/v1/conversation/stream?profile=kitchen&sample_rate=16000&audio_codec=opus&output=audio,text&audio_out=opus&output_sample_rate=24000
```
```

Edit the "Precedence" bullets (~lines 67-77):

```
Then point the device's WS URL at `?profile=kitchen`. Precedence:
- **LLM (model/base_url/api_key/system_prompt) and MCP tool servers**: always come from
  the profile when set — there's no device-side query param for these.
- **TTS**: the profile's `tts.engine`/`tts.voice` are used if set; otherwise the server-wide
  `default_tts_engine` applies. No per-request `?tts_engine=` override exists.
- **STT engine**: the profile's `stt.engine` is used if set; otherwise the server-wide
  `default_stt_engine` applies. No per-request `?stt_engine=` override exists — `?language=`
  still works, and if the profile's *name* matches a built-in language preset (`vi`, `en`,
  `multi`, `en_vi`) with no explicit `stt.engine`, that preset's engine/language is used.
- **Memory**: if `memory.enabled` is true on the profile, the server auto-extracts and
  later injects relevant memories into the system prompt for that profile — no device
  change needed.
```

- [ ] **Step 4: Update `rpi-assistant/integration.md`**

Apply the identical table/example/precedence edits as Step 3 (same doc content, RPi-specific copy — `output_sample_rate` defaults to `16000` there instead of `24000`, keep that difference; `qwen_omni` audio-native note on the removed `stt_engine` row should move to a plain sentence near the `profile` row instead, e.g. "a profile can pin `qwen_omni` for audio-native replies").

- [ ] **Step 5: Update `esp32-assistant/README.md`**

Edit the protocol block (lines 141-151):

```
## Protocol

The firmware connects to:

```
ws[s]://host:port/v1/conversation/stream
    ?profile=…&language=…
    &sample_rate=16000&audio_codec=opus
    &output=audio,text&audio_out=opus&output_sample_rate=16000
```

STT/TTS engine selection comes from the profile (or the server-wide default if no
profile is set) — there is no per-connection engine query param.
```

- [ ] **Step 6: Grep to confirm no stray references remain**

Run: `grep -rn "?stt_engine=\|&stt_engine=\|?tts_engine=\|&tts_engine=" docs/ rpi-assistant/*.md esp32-assistant/README.md`
Expected: no output (all matches cleaned up). Note `docs/api.md`'s `session_started` event fields table row (`stt_engine`, `stt_detail`, `tts_engine`, ...) is a **server → client reported value**, not a request param — leave that row alone, it's unaffected.

- [ ] **Step 7: Commit**

```bash
git add docs/api.md docs/device-integration.md rpi-assistant/integration.md esp32-assistant/README.md
git commit -m "$(cat <<'EOF'
docs: update device-integration guides for the new engine-resolution chain

Removes ?stt_engine=/?tts_engine= from every documented connect example
(no longer a valid override) and refreshes the GET /v1/system/config
JSON example to match the field moves from the last several commits.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: Add field metadata + `GET /v1/system/config/meta`

**Files:**
- Modify: `apps/api_gateway/app/services/system_config.py` (`EngineDefaults`, `ConversationTuningConfig`, `PreprocessingConfig` — add `Field(...)`)
- Modify: `apps/api_gateway/app/api/routes/system.py` (new endpoint)
- Test: `tests/unit/test_system_config_meta_route.py` (new)

**Interfaces:**
- Produces: `GET /v1/system/config/meta` → `{"success": true, "data": {"engines": {...}, "conversation": {...}, "preprocessing": {...}}}`, each inner dict keyed by field name with `{"label": str, "description": str, "subgroup": str|null, "unit": str|null, "multiline": bool}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_system_config_meta_route.py
from fastapi.testclient import TestClient

from app.main import app


def test_meta_endpoint_covers_all_three_remaining_groups():
    client = TestClient(app)
    data = client.get("/v1/system/config/meta").json()["data"]
    assert set(data.keys()) == {"engines", "conversation", "preprocessing"}


def test_meta_endpoint_has_no_stale_field_names():
    client = TestClient(app)
    data = client.get("/v1/system/config/meta").json()["data"]
    assert "conversation_stt_engine" not in data["conversation"]
    assert "conversation_tts_engine" not in data["conversation"]
    assert "pyannote_vad_model" not in data["preprocessing"]


def test_meta_entry_shape_for_a_representative_field():
    client = TestClient(app)
    data = client.get("/v1/system/config/meta").json()["data"]
    entry = data["engines"]["default_stt_engine"]
    assert entry["label"] == "Default STT engine"
    assert "standalone transcription" in entry["description"]
    assert entry["subgroup"] == "Engine selection"
    assert entry["unit"] is None
    assert entry["multiline"] is False


def test_meta_marks_the_system_prompt_field_multiline():
    client = TestClient(app)
    data = client.get("/v1/system/config/meta").json()["data"]
    assert data["conversation"]["conversation_system_prompt"]["multiline"] is True


def test_meta_groups_conversation_fields_into_four_subgroups():
    client = TestClient(app)
    data = client.get("/v1/system/config/meta").json()["data"]
    subgroups = {entry["subgroup"] for entry in data["conversation"].values()}
    assert subgroups == {"Timing & VAD", "STT", "TTS & Audio", "Language & Prompt"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_system_config_meta_route.py -v`
Expected: FAIL — 404 (endpoint doesn't exist yet)

- [ ] **Step 3: Add `Field(...)` metadata to every remaining `SystemConfig` field**

Edit `apps/api_gateway/app/services/system_config.py`, add `Field` to the pydantic import:

```python
from pydantic import BaseModel, Field
```

Replace `EngineDefaults`:

```python
class EngineDefaults(BaseModel):
    default_stt_engine: str = Field(
        default="vosk",
        title="Default STT engine",
        description="Used for standalone transcription (/v1/stt/transcribe, /v1/stt/stream) and for live voice conversations (unless overridden per-profile).",
        json_schema_extra={"subgroup": "Engine selection"},
    )
    default_tts_engine: str = Field(
        default="omnivoice",
        title="Default TTS engine",
        description="Used for live voice conversations and Livehost replies (unless overridden per-profile/TTS profile).",
        json_schema_extra={"subgroup": "Engine selection"},
    )
    default_tts_engine_voice: str = Field(
        default="",
        title="Default TTS voice",
        description="Optional preset voice for the default TTS engine. Leave empty to use the engine's own default voice.",
        json_schema_extra={"subgroup": "Engine selection"},
    )
    # Long-audio segmentation: split a clip into chunks and transcribe them in
    # parallel. Batch /v1/stt/transcribe only -- the live conversation flow
    # never uses this (utterances are already short). Previously its own
    # top-level "STT (Shared Settings)" group; folded in here once the
    # group's other 4 fields moved to env (see Settings) -- 3 fields didn't
    # warrant a standalone accordion.
    stt_segment_long_enabled: bool = Field(
        default=False,
        title="Enable long-audio segmentation",
        description="Split long recordings into chunks and transcribe them in parallel (batch /v1/stt/transcribe endpoint only; live conversation is unaffected).",
        json_schema_extra={"subgroup": "Long-audio segmentation (batch STT)"},
    )
    stt_segment_min_seconds: float = Field(
        default=30.0,
        title="Segmentation threshold (s)",
        description="Minimum clip duration before segmentation kicks in.",
        json_schema_extra={"subgroup": "Long-audio segmentation (batch STT)", "unit": "s"},
    )
    stt_segment_concurrency: int = Field(
        default=4,
        title="Segmentation concurrency",
        description="Max number of audio chunks transcribed in parallel per request.",
        json_schema_extra={"subgroup": "Long-audio segmentation (batch STT)"},
    )
```

Replace `ConversationTuningConfig`:

```python
class ConversationTuningConfig(BaseModel):
    conversation_silence_ms: int = Field(
        default=700,
        title="Silence to end turn (ms)",
        description="How long the user must stay silent before their turn is considered finished.",
        json_schema_extra={"subgroup": "Timing & VAD", "unit": "ms"},
    )
    conversation_min_silence_ms: int = Field(
        default=450,
        title="Minimum silence gap (ms)",
        description="Shortest silence gap the endpointer will treat as a pause (below this, it's ignored as noise).",
        json_schema_extra={"subgroup": "Timing & VAD", "unit": "ms"},
    )
    conversation_adaptive_full_ms: int = Field(
        default=3000,
        title="Adaptive full-silence window (ms)",
        description="Speech duration after which the required trailing silence grows toward its full value (longer utterances get more hang time).",
        json_schema_extra={"subgroup": "Timing & VAD", "unit": "ms"},
    )
    conversation_min_speech_ms: int = Field(
        default=300,
        title="Minimum speech duration (ms)",
        description="Shortest detected speech burst treated as an actual utterance (below this is ignored as noise).",
        json_schema_extra={"subgroup": "Timing & VAD", "unit": "ms"},
    )
    conversation_rms_threshold: float = Field(
        default=0.015,
        title="Speech volume threshold (RMS)",
        description="Minimum audio RMS level classified as speech vs. background noise. Tune per microphone/environment.",
        json_schema_extra={"subgroup": "Timing & VAD"},
    )
    conversation_preroll_ms: int = Field(
        default=600,
        title="Pre-roll buffer (ms)",
        description="Audio kept before speech onset is detected, so the very start of an utterance isn't clipped.",
        json_schema_extra={"subgroup": "Timing & VAD", "unit": "ms"},
    )
    conversation_barge_in_grace_ms: int = Field(
        default=500,
        title="Barge-in grace period (ms)",
        description="Ignore user speech for this long after the assistant starts talking, since the first frames the mic hears are usually the assistant's own audio echoing back. 0 disables the grace.",
        json_schema_extra={"subgroup": "Timing & VAD", "unit": "ms"},
    )
    conversation_max_utterance_ms: int = Field(
        default=30000,
        title="Max utterance length (ms)",
        description="Hard cap on a single user turn's length; forces an end-of-turn even if the user keeps talking.",
        json_schema_extra={"subgroup": "Timing & VAD", "unit": "ms"},
    )
    conversation_goodbye_text: str = Field(
        default="Hẹn gặp lại nha!",
        title="Goodbye phrase",
        description="Spoken when a conversation ends gracefully (e.g. user says goodbye).",
        json_schema_extra={"subgroup": "Language & Prompt"},
    )
    conversation_fast_stt_engine: str = Field(
        default="",
        title="Fast STT engine",
        description="Optional low-latency engine used only for short utterances (≤ Fast STT max ms). Independent of Default STT engine — no fallback relationship, just an opt-in fast path.",
        json_schema_extra={"subgroup": "STT"},
    )
    conversation_fast_stt_max_ms: int = Field(
        default=1500,
        title="Fast STT max utterance (ms)",
        description="Utterances at or under this length use the Fast STT engine above instead of the resolved default.",
        json_schema_extra={"subgroup": "STT", "unit": "ms"},
    )
    conversation_streaming_stt: bool = Field(
        default=False,
        title="Enable streaming STT",
        description="Transcribe audio incrementally as it arrives instead of waiting for the full utterance.",
        json_schema_extra={"subgroup": "STT"},
    )
    conversation_streaming_chunk_ms: int = Field(
        default=1000,
        title="Streaming chunk size (ms)",
        description="Audio chunk size fed to the STT engine when streaming STT is enabled.",
        json_schema_extra={"subgroup": "STT", "unit": "ms"},
    )
    conversation_tts_lookahead: int = Field(
        default=3,
        title="TTS sentence lookahead",
        description="Number of upcoming sentences synthesized ahead of playback, to hide TTS latency.",
        json_schema_extra={"subgroup": "TTS & Audio"},
    )
    conversation_opus_pace: bool = Field(
        default=False,
        title="Pace Opus playback",
        description="Rate-limit outgoing Opus frames to real playback speed instead of sending as fast as generated (smoother client-side buffering).",
        json_schema_extra={"subgroup": "TTS & Audio"},
    )
    conversation_opus_prebuffer_frames: int = Field(
        default=5,
        title="Opus prebuffer frames",
        description="Number of Opus frames buffered client-side before playback starts.",
        json_schema_extra={"subgroup": "TTS & Audio"},
    )
    conversation_language: str = Field(
        default="vi",
        title="Conversation language",
        description="Default language for STT/TTS when a profile doesn't specify one. Empty means auto-detect where supported.",
        json_schema_extra={"subgroup": "Language & Prompt"},
    )
    llm_timeout_seconds: float = Field(
        default=60.0,
        title="LLM request timeout (s)",
        description="Shared HTTP timeout for every LLM call (chat responses, memory extraction/compaction, embeddings).",
        json_schema_extra={"subgroup": "Language & Prompt", "unit": "s"},
    )
    conversation_system_prompt: str = Field(
        default=(
            "You are a helpful, concise voice assistant. Reply in the user's language, "
            "in 2-4 short sentences suitable for being spoken aloud. "
            "Your reply is read aloud by text-to-speech, so write plain speakable prose only: "
            "do NOT use emojis, emoticons, kaomoji, or decorative/pictographic symbols, "
            "and avoid markdown, bullet points, or code blocks. "
            "Write in complete, flowing sentences ending with a normal period. "
            "Do NOT use ellipses (…) or trailing dots for dramatic pauses, and do NOT put "
            "line breaks inside a thought or split dialogue across multiple lines."
        ),
        title="System prompt",
        description="Base instructions given to the LLM for every conversation turn (prepended to any profile-specific prompt).",
        json_schema_extra={"subgroup": "Language & Prompt", "multiline": True},
    )
```

Replace `PreprocessingConfig`:

```python
class PreprocessingConfig(BaseModel):
    stt_vad_enabled: bool = Field(
        default=False, title="Enable VAD",
        description="Gate non-speech regions out of audio before transcription.",
    )
    stt_vad_backend: str = Field(
        default="energy", title="VAD backend",
        description="Which voice-activity-detection algorithm to use: energy (always available), silero, or pyannote (both need extra dependencies/model download).",
    )
    stt_noise_reduce_enabled: bool = Field(
        default=False, title="Enable noise reduction",
        description="Apply noise reduction to audio before transcription.",
    )
    stt_noise_reduce_amount: float = Field(
        default=0.85, title="Noise reduction amount",
        description="Strength of noise reduction, from 0 (none) to 1 (maximum).",
    )
```

- [ ] **Step 4: Add the `GET /v1/system/config/meta` endpoint**

Edit `apps/api_gateway/app/api/routes/system.py`, add the import and the new route (place it right before `@router.get("/system/config")`):

```python
from app.services.system_config import ConversationTuningConfig, EngineDefaults, PreprocessingConfig, SystemConfig, system_config_store
```

(replaces the existing `from app.services.system_config import SystemConfig, system_config_store` import)

```python
def _field_meta(model: type[BaseModel]) -> dict[str, dict]:
    meta = {}
    for name, info in model.model_fields.items():
        extra = info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}
        meta[name] = {
            "label": info.title or name,
            "description": info.description or "",
            "subgroup": extra.get("subgroup"),
            "unit": extra.get("unit"),
            "multiline": extra.get("multiline", False),
        }
    return meta


@router.get("/system/config/meta")
async def get_system_config_meta() -> dict:
    return {
        "success": True,
        "data": {
            "engines": _field_meta(EngineDefaults),
            "conversation": _field_meta(ConversationTuningConfig),
            "preprocessing": _field_meta(PreprocessingConfig),
        },
    }
```

(`_field_meta` needs `BaseModel` in scope — add `from pydantic import BaseModel` to `system.py`'s imports alongside the existing `import pydantic`)

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/unit/test_system_config_meta_route.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Run the full config test suite to confirm the `Field(...)` additions didn't change defaults**

Run: `pytest tests/unit/test_system_config_store.py tests/unit/test_system_config_routes.py -v`
Expected: all still PASS — `Field(default=...)` is behaviorally identical to a bare `= default` assignment for every existing default-value assertion.

- [ ] **Step 7: Commit**

```bash
git add apps/api_gateway/app/services/system_config.py apps/api_gateway/app/api/routes/system.py tests/unit/test_system_config_meta_route.py
git commit -m "$(cat <<'EOF'
feat(config): add field metadata + GET /v1/system/config/meta

Human-readable label/description/subgroup/unit per field, introspected
from the Pydantic model (not hand-maintained) so it can't drift out of
sync with the schema. JSON shape of SystemConfig itself is unchanged --
this is a separate, additive endpoint. Task 12 wires the admin UI to it.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 12: Rewrite `system-config.js` (3 groups, sub-blocks, textarea, per-group Save) + CSS

**Files:**
- Modify: `apps/api_gateway/app/static/js/system-config.js` (full rewrite)
- Modify: `apps/api_gateway/app/static/index.html:957-967` (Settings pane markup)
- Modify: `apps/api_gateway/app/static/styles.css` (add accordion/sub-block styles)

**Interfaces:**
- Produces: 3 styled `<details>` accordions (Engine Defaults / Conversation Tuning / Preprocessing), each with metadata-driven labels/descriptions, sub-blocks where applicable, a `<textarea>` for `conversation_system_prompt`, and its own Save button + status line.

- [ ] **Step 1: Update the Settings pane markup in `index.html`**

Edit `apps/api_gateway/app/static/index.html:957-967`, drop the single shared Save button (each group now has its own):

```html
            <div class="subtab-pane" id="subtab-system-settings">
              <section class="card">
                <h2>System settings</h2>
                <p class="hint">Engine choices and conversation tuning. Changes take effect immediately — no restart needed. Deployment-time settings (model paths, secrets) are configured via environment variables — see docs/api.md.</p>
                <div id="sys-config-groups"></div>
              </section>
            </div>
```

- [ ] **Step 2: Add accordion/sub-block CSS**

Edit `apps/api_gateway/app/static/styles.css`, add a new section after the existing `CARD` block (after line ~411, before `BUTTONS`):

```css
/* ================================================================
   SETTINGS ACCORDION
   ================================================================ */

.settings-group {
  border: 1px solid var(--line);
  border-radius: var(--r-md);
  margin-bottom: 12px;
  overflow: hidden;
}

.settings-group summary {
  cursor: pointer;
  padding: 12px 16px;
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.02em;
  color: var(--text);
  list-style: none;
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: rgba(4, 9, 14, 0.5);
  transition: background 120ms ease, color 120ms ease;
}
.settings-group summary::-webkit-details-marker {
  display: none;
}
.settings-group summary::after {
  content: "▸";
  color: var(--muted);
  transition: transform 120ms ease, color 120ms ease;
}
.settings-group[open] summary::after {
  transform: rotate(90deg);
  color: var(--accent);
}
.settings-group summary:hover {
  color: var(--accent);
}

.settings-group-body {
  padding: 16px;
  border-top: 1px solid var(--line);
}

.field-subgroup {
  margin-bottom: 18px;
}
.field-subgroup:last-child {
  margin-bottom: 0;
}
.field-subgroup h3.sub {
  margin-top: 0;
}
.field-subgroup .fields-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px 16px;
  align-items: start;
}
.field-subgroup .fields-grid .field-full {
  grid-column: 1 / -1;
}

.field-desc {
  margin: -3px 0 6px;
  color: var(--muted);
  font-size: 11px;
  font-weight: 400;
  text-transform: none;
  letter-spacing: normal;
  line-height: 1.4;
}
```

- [ ] **Step 3: Rewrite `system-config.js`**

Replace `apps/api_gateway/app/static/js/system-config.js` in full:

```javascript
import { el, print } from "./helpers.js";

export async function loadBaseContext() {
  try {
    const body = await (await fetch("/v1/system/config")).json();
    el("sys-base-context").value = body.data.base_context || "";
  } catch (error) {
    print(el("sys-base-context-status"), String(error), true);
  }
}

export async function saveBaseContext() {
  const status = el("sys-base-context-status");
  try {
    const resp = await fetch("/v1/system/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base_context: el("sys-base-context").value }),
    });
    const body = await resp.json();
    if (!resp.ok) { print(status, body.detail || JSON.stringify(body), true); return; }
    status.classList.remove("error");
    status.textContent = "Saved ✓";
  } catch (error) {
    print(status, String(error), true);
  }
}
if (el("sys-base-context-save")) {
  el("sys-base-context-save").addEventListener("click", saveBaseContext);
  loadBaseContext();
}

// OpenRouter no longer has a single system-wide key -- each qwen3_asr_or/
// whisper_or model added in Model Registry carries its own api_key (see
// model-registry.js), so there is no per-system key panel here anymore.

const GROUPS = [
  { key: "engines", label: "Engine Defaults", open: true },
  { key: "conversation", label: "Conversation Tuning", open: false },
  { key: "preprocessing", label: "Preprocessing (VAD/Noise)", open: false },
];

// Engine-name fields must be picked from the live engine lists, not typed
// free-text (a typo'd engine only fails at request time). kind selects which
// list to render from; optional means "" is a valid value.
const ENGINE_SELECT_FIELDS = {
  "engines.default_stt_engine": { kind: "stt" },
  "engines.default_tts_engine": { kind: "tts" },
  "conversation.conversation_fast_stt_engine": { kind: "stt", optional: true },
};

// Voice list depends on the engine chosen in the sibling select, so it is
// rendered as a shell here and (re)populated by populateVoiceOptions().
const VOICE_FIELD = "engines.default_tts_engine_voice";
const VOICE_SELECT_ID = "sys-engines-default_tts_engine_voice";
const TTS_ENGINE_SELECT_ID = "sys-engines-default_tts_engine";

// Default LLM is NOT a system_config field like default_stt_engine/
// default_tts_engine -- the conversation LLM is a single Model Registry
// kind="llm" row with is_default=true (see responder.py's _active_llm_entry),
// so this widget lives outside the generic schema-driven field loop above and
// is (re)populated by populateDefaultLlmField(), same pattern as the voice
// select. Selecting a different row PATCHes it is_default=true (and enabled=
// true, so picking a default also makes it selectable) immediately -- the
// backend enforces at most one is_default llm row, not one enabled llm row
// anymore (multiple llm rows can be enabled/selectable per-profile at once;
// see model_registry/store.py's _disable_other_llm_defaults), not bundled
// into the group Save button.
const DEFAULT_LLM_FIELD_ID = "sys-default-llm";

function fieldInputType(value) {
  if (typeof value === "boolean") return "checkbox";
  if (typeof value === "number") return "number";
  return "text";
}

function renderEngineSelect(id, current, engines, optional) {
  const options = [];
  if (optional) options.push(`<option value=""${current === "" ? " selected" : ""}>(none)</option>`);
  let hasCurrent = optional && current === "";
  for (const e of engines) {
    const selected = e.engine === current;
    if (selected) hasCurrent = true;
    // Unavailable engines stay visible but unpickable -- unless one is the
    // saved value, which must survive a round-trip through Save.
    const disabled = e.available || selected ? "" : " disabled";
    const label = e.available ? e.engine : `${e.engine} (not installed)`;
    options.push(`<option value="${e.engine}"${selected ? " selected" : ""}${disabled}>${label}</option>`);
  }
  if (!hasCurrent && current) options.unshift(`<option value="${current}" selected>${current} (unknown)</option>`);
  return `<select id="${id}">${options.join("")}</select>`;
}

async function populateVoiceOptions() {
  const voiceSel = el(VOICE_SELECT_ID);
  const engineInput = el(TTS_ENGINE_SELECT_ID);
  if (!voiceSel || !engineInput) return;
  const current = voiceSel.value;
  let voices = [];
  try {
    const body = await (await fetch(`/v1/tts/voices?engine=${encodeURIComponent(engineInput.value)}`)).json();
    voices = body.data?.voices || [];
  } catch (error) {
    /* voices optional */
  }
  voiceSel.innerHTML = '<option value="">(auto)</option>';
  voices.forEach((v) => {
    const opt = document.createElement("option");
    opt.value = v.voice;
    opt.textContent = v.label;
    voiceSel.appendChild(opt);
  });
  if (current && !voices.some((v) => v.voice === current)) {
    const opt = document.createElement("option");
    opt.value = current;
    opt.textContent = `${current} (current)`;
    voiceSel.appendChild(opt);
  }
  voiceSel.value = current;
}

function fieldLabel(meta, field, unit) {
  const label = meta?.label || field;
  return unit ? `${label} (${unit})` : label;
}

function renderField(groupKey, field, value, meta, engineLists) {
  const id = `sys-${groupKey}-${field}`;
  const key = `${groupKey}.${field}`;
  const spec = ENGINE_SELECT_FIELDS[key];
  const desc = meta?.description ? `<p class="field-desc">${meta.description}</p>` : "";
  const isFull = meta?.multiline;
  const wrapClass = isFull ? "field field-full" : "field";

  if (spec && engineLists[spec.kind] && engineLists[spec.kind].length) {
    return `<label class="${wrapClass}">${fieldLabel(meta, field)}${desc}
      ${renderEngineSelect(id, String(value), engineLists[spec.kind], spec.optional)}
    </label>`;
  }
  if (key === VOICE_FIELD) {
    return `<label class="${wrapClass}">${fieldLabel(meta, field)}${desc}
      <select id="${id}"><option value="${value}" selected>${value || "(auto)"}</option></select>
    </label>`;
  }
  if (meta?.multiline) {
    return `<label class="${wrapClass}">${fieldLabel(meta, field)}${desc}
      <textarea id="${id}" rows="4">${value}</textarea>
    </label>`;
  }
  const type = fieldInputType(value);
  const checked = type === "checkbox" && value ? "checked" : "";
  const val = type === "checkbox" ? "" : `value="${String(value)}"`;
  return `<label class="${wrapClass}">${fieldLabel(meta, field, meta?.unit)}${desc}
    <input type="${type}" id="${id}" ${val} ${checked} />
  </label>`;
}

function renderGroupFields(groupKey, groupValue, groupMeta, engineLists) {
  const entries = Object.entries(groupValue);
  const subgroups = new Map(); // subgroup label (or null) -> field entries, insertion order preserved
  for (const [field, value] of entries) {
    const meta = groupMeta?.[field];
    const sub = meta?.subgroup || null;
    if (!subgroups.has(sub)) subgroups.set(sub, []);
    subgroups.get(sub).push([field, value, meta]);
  }
  const blocks = [];
  for (const [sub, fields] of subgroups) {
    const heading = sub ? `<h3 class="sub">${sub}</h3>` : "";
    const rendered = fields
      .map(([field, value, meta]) => renderField(groupKey, field, value, meta, engineLists))
      .join("\n");
    blocks.push(`<div class="field-subgroup">${heading}<div class="fields-grid">${rendered}</div></div>`);
  }
  return blocks.join("\n");
}

async function fetchEngineList(url) {
  try {
    const body = await (await fetch(url)).json();
    return body.data || null;
  } catch (error) {
    return null; // fall back to a plain text input for engine fields
  }
}

export async function loadSystemConfigGroups() {
  const root = el("sys-config-groups");
  if (!root) return;
  const [body, meta, stt, tts] = await Promise.all([
    fetch("/v1/system/config").then((r) => r.json()),
    fetch("/v1/system/config/meta").then((r) => r.json()),
    fetchEngineList("/v1/stt/engines"),
    fetchEngineList("/v1/tts/engines"),
  ]);
  const engineLists = { stt: stt || [], tts: tts || [] };
  root.innerHTML = GROUPS.map(
    (g) => `<details class="settings-group" ${g.open ? "open" : ""}>
      <summary>${g.label}</summary>
      <div class="settings-group-body">
        ${renderGroupFields(g.key, body.data[g.key], meta.data[g.key], engineLists)}
        ${g.key === "engines" ? `<div class="field-subgroup"><h3 class="sub">Engine selection</h3><div class="fields-grid"><label class="field">Default LLM
          <select id="${DEFAULT_LLM_FIELD_ID}" disabled><option>loading…</option></select>
        </label></div></div>` : ""}
        <div class="actions end">
          <button data-save-group="${g.key}">Save</button>
        </div>
        <p class="meta" data-status-group="${g.key}"></p>
      </div>
    </details>`
  ).join("\n");
  populateVoiceOptions();
  populateDefaultLlmField();
  const engineSel = el(TTS_ENGINE_SELECT_ID);
  // innerHTML above recreated the element, so a fresh listener each load.
  if (engineSel) engineSel.addEventListener("change", populateVoiceOptions);
  root.querySelectorAll("[data-save-group]").forEach((btn) => {
    btn.addEventListener("click", () => saveSystemConfigGroup(btn.dataset.saveGroup));
  });
}

// Selecting a different row PATCHes it is_default=true (and enabled=true)
// right away (see the DEFAULT_LLM_FIELD_ID comment above) -- this select has
// no "Save" step of its own, so it must reflect committed state immediately
// after the PATCH.
async function populateDefaultLlmField() {
  const sel = el(DEFAULT_LLM_FIELD_ID);
  if (!sel) return;
  let entries = [];
  try {
    const body = await (await fetch("/v1/model_registry")).json();
    entries = (body.data || []).filter((e) => e.kind === "llm");
  } catch (error) {
    sel.innerHTML = '<option value="">(failed to load)</option>';
    return;
  }
  if (!entries.length) {
    sel.innerHTML = '<option value="">(none configured — add one in Model Registry)</option>';
    sel.disabled = true;
    return;
  }
  const current = entries.find((e) => e.is_default);
  sel.innerHTML = [
    !current ? '<option value="" selected>(no default set)</option>' : "",
    ...entries.map(
      (e) =>
        `<option value="${e.id}"${e === current ? " selected" : ""}>${e.label} — ${e.model_id}</option>`
    ),
  ].join("");
  sel.disabled = false;
  sel.onchange = async () => {
    if (!sel.value) return;
    sel.disabled = true;
    try {
      await fetch(`/v1/model_registry/${encodeURIComponent(sel.value)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_default: true, enabled: true }),
      });
    } finally {
      populateDefaultLlmField();
    }
  };
}

export async function saveSystemConfigGroup(groupKey) {
  const status = el(`[data-status-group="${groupKey}"]`) || document.querySelector(`[data-status-group="${groupKey}"]`);
  try {
    const current = await (await fetch("/v1/system/config")).json();
    const groupPayload = current.data[groupKey];
    for (const field of Object.keys(groupPayload)) {
      const input = el(`sys-${groupKey}-${field}`);
      if (!input) continue;
      groupPayload[field] =
        input.type === "checkbox"
          ? input.checked
          : input.type === "number"
            ? Number(input.value)
            : input.value;
    }
    const resp = await fetch("/v1/system/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [groupKey]: groupPayload }),
    });
    const body = await resp.json();
    if (!resp.ok) { print(status, body.detail || JSON.stringify(body), true); return; }
    status.classList.remove("error");
    status.textContent = "Saved ✓ (applies immediately, no restart needed)";
    await loadSystemConfigGroups();
  } catch (error) {
    print(status, String(error), true);
  }
}

if (el("sys-config-groups")) {
  loadSystemConfigGroups();
}
```

(note: `document.querySelector` on a bracket-attribute selector works fine directly — the `el()` helper likely only does `document.getElementById`, so the `el(\`[data-status-group="${groupKey}"]\`)` half of that line is dead and can be simplified to just `document.querySelector(...)`; check `helpers.js`'s `el()` implementation first and simplify this line to whichever single call actually works)

- [ ] **Step 4: Manual browser check**

Start the dev server, open the admin console, go to System → Settings. Confirm:
- 3 accordions render: "Engine Defaults" (open by default), "Conversation Tuning", "Preprocessing (VAD/Noise)"
- Engine Defaults shows an "Engine selection" sub-heading (3 engine fields + Default LLM) and a "Long-audio segmentation (batch STT)" sub-heading (3 fields)
- Conversation Tuning shows 4 sub-headings: Timing & VAD, STT, TTS & Audio, Language & Prompt
- Every field shows a human-readable label (not a raw snake_case name) and a description line underneath
- `conversation_system_prompt` renders as a multi-line textarea spanning the full width
- Each accordion has its own Save button; changing a field in one group and clicking that group's Save doesn't touch other groups' unsaved edits
- No browser console errors

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/static/js/system-config.js apps/api_gateway/app/static/index.html apps/api_gateway/app/static/styles.css
git commit -m "$(cat <<'EOF'
feat(ui): restructure System Settings into 3 labeled, sub-grouped accordions

Replaces raw snake_case field names with metadata-driven labels/
descriptions (from GET /v1/system/config/meta), groups Conversation
Tuning's 19 fields into 4 sub-blocks, renders the system prompt as a
textarea, styles the accordion to match the app's design system, and
switches from one shared Save button to one per group.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 13: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full backend test suite**

Run: `cd apps/api_gateway && python -m pytest ../../tests -x -q` (adjust the invocation to however the project's CI/pre-commit normally runs the suite — check for a `pytest.ini`/`pyproject.toml` `[tool.pytest.ini_options]` `testpaths` setting, or a documented command in `CLAUDE.md`/`README.md`, and use that instead if it differs)
Expected: 0 failures. If anything fails, it's almost certainly a test file this plan didn't anticipate touching (e.g. another test importing a removed field) — fix it following the same pattern as the task that removed the field, don't skip/xfail it.

- [ ] **Step 2: Grep for any remaining dead references**

Run:
```bash
grep -rn "stt_local\b" apps/api_gateway/app --include="*.py" | grep -v "^apps/api_gateway/app/services/model_registry/seed.py"
grep -rn "conversation_stt_engine\|conversation_tts_engine\|extra_warmup_stt_engines\|extra_warmup_tts_engines" apps/api_gateway/app --include="*.py"
grep -rn "q_engine\|q\.get(\"stt_engine\")\|q\.get(\"tts_engine\")" apps/api_gateway/app --include="*.py"
```
Expected: no output from any of the 3 (the `seed.py` exclusion in the first command is intentional — `get_raw_group("stt_local")` there is a legacy migration reading raw historical JSON, unaffected by the live schema change, see Task 6's notes).

- [ ] **Step 3: Manual end-to-end browser check**

Start the dev server. In the browser:
1. System → Settings: confirm the 3-group layout from Task 12 still works after all the backend changes.
2. Chat: start a conversation with no profile selected, confirm it works using the server default engine (Task 8's dropdown removal + Task 7's chain).
3. Chat: create/select a profile with a pinned STT engine, confirm the conversation uses that engine (check the `session_started` event's `stt_engine` field in the browser console/network tab, or via any on-page indicator).
4. Livehost: same check as steps 2-3, on the Livehost page (Task 9).

- [ ] **Step 4: Report status**

If all of Steps 1–3 pass, the restructure is complete — no further action. If Step 1 turns up unanticipated failures, fix them (each fix is its own small commit, following the "no query-param engine override" global constraint and the field-classification table in the design spec) before considering the plan done.
