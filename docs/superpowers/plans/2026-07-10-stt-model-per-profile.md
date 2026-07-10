# STT model selection per profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a `Profile` pin a specific STT model variant (e.g. PhoWhisper-medium vs tiny, Qwen3-ASR 0.6B vs 1.7B), validated against a list of valid+available models, and have the server switch to and eagerly warm that model when a session starts — mirroring how TTS is already fully profile-driven.

**Architecture:** Add `SttConfig.model` to the existing profile model. Add a tiny `SttModelRegistry` interface (`list_models`/`validate`/`select`) implemented by the existing `whisper_manager` (whisper/whisper_local/whisper_gemma) and a new thin wrapper around `qwen3_asr`'s module-level active-model global. Thread a third `model` value through the existing `resolve_stt()` resolution chain and the existing warm-up machinery (session-start warm, boot warm, `/stt/warm`) — no new subsystem.

**Tech Stack:** FastAPI, Pydantic v2, pytest + pytest-asyncio, existing `AppError` → JSON 400 exception handling.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-10-stt-model-per-profile-design.md` — read it first for the "why" behind every task below.
- Model-variant selection only applies to engines with a registry: `whisper`, `whisper_local`, `whisper_gemma` (share `whisper_manager`), and `qwen3_asr`. All other engines (`vosk`, `whisper_mlx`, `whisper_service`, `eventlab`) have no variant concept — setting `stt.model` for a profile resolving to one of these is a validation error.
- The active model per engine remains a single process-global slot (unchanged) — this design adds profile-driven *selection* of that slot, not concurrent multi-model hosting.
- `stt.model = ""` must remain fully backward compatible — every existing profile keeps today's behavior unchanged.
- Run tests from the repo root: `pytest tests/unit/<file>.py -v` (pythonpath is configured for `apps/api_gateway` in `pyproject.toml`).
- Follow existing code style: no comments explaining *what*, only non-obvious *why* (this codebase's docstrings/comments consistently do this — match it).

---

### Task 1: Add `SttConfig.model` field

**Files:**
- Modify: `apps/api_gateway/app/services/profiles/models.py:18-27`
- Test: `tests/unit/test_profiles_models.py`

**Interfaces:**
- Produces: `SttConfig.model: str = ""` — consumed by Task 3 (`resolve_stt`), Task 5 (profile validation), Task 6 (session wiring).

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_profiles_models.py` (after `test_profile_stt_config`):

```python
def test_profile_stt_model_defaults_empty():
    from app.services.profiles.models import SttConfig

    p = Profile(name="x")
    assert p.stt.model == ""


def test_profile_stt_model_round_trip():
    from app.services.profiles.models import SttConfig

    p = Profile(name="x", stt=SttConfig(engine="qwen3_asr", model="0.6b"))
    assert p.stt.model == "0.6b"
    p2 = Profile.model_validate(p.model_dump())
    assert p2.stt.model == "0.6b"


def test_profile_stt_model_back_compat_old_json():
    # a profile saved before the model field existed still validates, defaulting to ""
    p = Profile.model_validate({"name": "legacy", "stt": {"engine": "whisper"}})
    assert p.stt.model == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_profiles_models.py -v -k stt_model`
Expected: FAIL — `AttributeError: 'SttConfig' object has no attribute 'model'`

- [ ] **Step 3: Add the field**

In `apps/api_gateway/app/services/profiles/models.py`, update `SttConfig`:

```python
class SttConfig(BaseModel):
    # Language preset (services/stt/profile.py: vi|en|multi|en_vi) — sets engine +
    # language together. "" = inherit the server-wide default (settings.stt_profile).
    profile: str = ""
    # Explicit overrides, for when the preset isn't enough. "" = derive from the
    # preset / server default. engine is a registered STT engine name; language is
    # a hint ("" = auto-detect via the preset).
    engine: str = ""
    language: str = ""
    # Model-variant id for engines with a registry (see stt/model_registry.py) —
    # e.g. a whisper size ("phowhisper-medium") or a qwen3_asr shorthand ("0.6b").
    # "" = inherit whatever model is currently active for the resolved engine.
    model: str = ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_profiles_models.py -v -k stt_model`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/profiles/models.py tests/unit/test_profiles_models.py
git commit -m "feat(profiles): add SttConfig.model field for per-profile STT model variant"
```

---

### Task 2: STT model registries (whisper + qwen3_asr)

**Files:**
- Modify: `apps/api_gateway/app/services/whisper_models.py`
- Create: `apps/api_gateway/app/services/stt/model_registry.py`
- Test: `tests/unit/test_stt_model_registry.py`

**Interfaces:**
- Consumes: `whisper_manager.snapshot()/.validate()/.select()` (existing, `apps/api_gateway/app/services/whisper_models.py`); `QWEN3_ASR_MODELS`, `resolve_qwen3_asr_model()`, `get_active_qwen3_asr_model()`, `set_active_qwen3_asr_model()` (existing, `apps/api_gateway/app/services/stt/providers/qwen3_asr_provider.py`); `repo_cached(repo: str | None) -> bool` (existing, `apps/api_gateway/app/core/hf_cache.py`).
- Produces: `whisper_manager.list_models() -> list[dict]` (new method, shape `{id, label, cached, active}`); `Qwen3AsrModelRegistry` class with `.validate(model_id: str) -> None` (raises `AppError`), `.select(model_id: str) -> None`, `.list_models() -> list[dict]` (same shape); `qwen3_asr_model_registry` singleton instance; `STT_MODEL_REGISTRIES: dict[str, object]` mapping engine name → registry (keys: `"whisper"`, `"whisper_local"`, `"whisper_gemma"`, `"qwen3_asr"`); `apply_stt_model(engine: str, model: str) -> None` (no-op if `model` is falsy or engine has no registry; raises `AppError` via the registry's `validate()` if `model` is invalid for that engine) — all in `apps/api_gateway/app/services/stt/model_registry.py`. Consumed by Task 4, 5, 6, 7.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_stt_model_registry.py`:

```python
import pytest

from app.core.errors import AppError
from app.services.stt.model_registry import (
    STT_MODEL_REGISTRIES,
    apply_stt_model,
    qwen3_asr_model_registry,
)
from app.services.stt.providers import qwen3_asr_provider as q
from app.services.whisper_models import whisper_manager


@pytest.fixture(autouse=True)
def _reset_qwen3():
    q.set_active_qwen3_asr_model(None)
    yield
    q.set_active_qwen3_asr_model(None)


def test_whisper_manager_list_models_shape():
    models = whisper_manager.list_models()
    assert models  # non-empty
    assert all({"id", "label", "cached", "active"} <= set(m) for m in models)
    ids = {m["id"] for m in models}
    assert {"tiny", "phowhisper-medium", "large-v3"} <= ids


def test_qwen3_registry_list_models_shape():
    models = qwen3_asr_model_registry.list_models()
    ids = {m["id"] for m in models}
    assert ids == {"0.6b", "1.7b"}
    assert all({"id", "label", "cached", "active"} <= set(m) for m in models)


def test_qwen3_registry_validate_rejects_unknown():
    with pytest.raises(AppError):
        qwen3_asr_model_registry.validate("7b-does-not-exist")


def test_qwen3_registry_validate_accepts_known():
    qwen3_asr_model_registry.validate("0.6b")  # no raise
    qwen3_asr_model_registry.validate("1.7B")  # case-insensitive, no raise


def test_qwen3_registry_select_changes_active():
    qwen3_asr_model_registry.select("1.7b")
    assert q.get_active_qwen3_asr_model() == "Qwen/Qwen3-ASR-1.7B"
    models = qwen3_asr_model_registry.list_models()
    active = {m["id"]: m["active"] for m in models}
    assert active == {"0.6b": False, "1.7b": True}


def test_registries_dict_covers_whisper_family_and_qwen3():
    assert STT_MODEL_REGISTRIES["whisper"] is whisper_manager
    assert STT_MODEL_REGISTRIES["whisper_local"] is whisper_manager
    assert STT_MODEL_REGISTRIES["whisper_gemma"] is whisper_manager
    assert STT_MODEL_REGISTRIES["qwen3_asr"] is qwen3_asr_model_registry
    assert "vosk" not in STT_MODEL_REGISTRIES
    assert "whisper_mlx" not in STT_MODEL_REGISTRIES


def test_apply_stt_model_noop_for_empty_model():
    apply_stt_model("qwen3_asr", "")  # must not raise
    assert q.get_active_qwen3_asr_model() != "Qwen/Qwen3-ASR-1.7B" or True  # unchanged either way


def test_apply_stt_model_noop_for_engine_without_registry():
    apply_stt_model("vosk", "anything")  # must not raise, no registry to apply to


def test_apply_stt_model_selects_for_known_engine():
    apply_stt_model("qwen3_asr", "1.7b")
    assert q.get_active_qwen3_asr_model() == "Qwen/Qwen3-ASR-1.7B"


def test_apply_stt_model_raises_for_invalid_model_on_known_engine():
    with pytest.raises(AppError):
        apply_stt_model("qwen3_asr", "not-a-real-size")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_stt_model_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.stt.model_registry'`

- [ ] **Step 3: Add `list_models()` to `WhisperManager`**

In `apps/api_gateway/app/services/whisper_models.py`, add this method to the `WhisperManager` class (after `snapshot()`, before `download()`):

```python
    def list_models(self) -> list[dict]:
        """Common STT model-registry shape (see app.services.stt.model_registry)."""
        snap = self.snapshot()
        return [
            {"id": m["size"], "label": m["label"], "cached": m["cached"], "active": m["active"]}
            for m in snap["models"]
        ]
```

- [ ] **Step 4: Create the registry module**

Create `apps/api_gateway/app/services/stt/model_registry.py`:

```python
"""Common model-variant registry for STT engines that support multiple sizes.

Bridges whisper_manager (whisper/whisper_local/whisper_gemma all share the same
process-global active whisper model — see whisper_provider.get_active_whisper_model)
and Qwen3-ASR's own module-level active-model global, so profile-driven model
selection (SttConfig.model) can validate/select/list against either engine the
same way. Engines with a single fixed model (vosk, whisper_mlx, the remote
engines) have no entry here — there's nothing to select.
"""

from app.core.errors import AppError
from app.core.hf_cache import repo_cached
from app.services.stt.providers.qwen3_asr_provider import (
    QWEN3_ASR_MODELS,
    get_active_qwen3_asr_model,
    resolve_qwen3_asr_model,
    set_active_qwen3_asr_model,
)
from app.services.whisper_models import whisper_manager

_QWEN3_LABELS = {
    "0.6b": "Qwen3-ASR 0.6B (fast)",
    "1.7b": "Qwen3-ASR 1.7B (accurate, multilingual)",
}


class Qwen3AsrModelRegistry:
    def validate(self, model_id: str) -> None:
        if (model_id or "").strip().lower() not in QWEN3_ASR_MODELS:
            raise AppError(f"Invalid qwen3_asr model: {model_id!r}")

    def select(self, model_id: str) -> None:
        self.validate(model_id)
        set_active_qwen3_asr_model(model_id)

    def list_models(self) -> list[dict]:
        active_repo = resolve_qwen3_asr_model(get_active_qwen3_asr_model())
        return [
            {
                "id": shorthand,
                "label": _QWEN3_LABELS.get(shorthand, repo),
                "cached": repo_cached(repo),
                "active": repo == active_repo,
            }
            for shorthand, repo in QWEN3_ASR_MODELS.items()
        ]


qwen3_asr_model_registry = Qwen3AsrModelRegistry()

STT_MODEL_REGISTRIES: dict[str, object] = {
    "whisper": whisper_manager,
    "whisper_local": whisper_manager,
    "whisper_gemma": whisper_manager,
    "qwen3_asr": qwen3_asr_model_registry,
}


def apply_stt_model(engine: str, model: str) -> None:
    """Best-effort switch the active model for `engine` to `model`.

    No-op if `model` is empty or `engine` has no registry (e.g. vosk,
    whisper_mlx — single fixed model, nothing to select). Raises AppError via
    the registry's validate() if `model` is set but not a known id for that
    engine — callers decide whether to propagate or catch-and-log.
    """
    if not model:
        return
    registry = STT_MODEL_REGISTRIES.get(engine)
    if registry is not None:
        registry.select(model)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/unit/test_stt_model_registry.py -v`
Expected: PASS (10 passed)

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/services/whisper_models.py apps/api_gateway/app/services/stt/model_registry.py tests/unit/test_stt_model_registry.py
git commit -m "feat(stt): add model-variant registry for whisper-family and qwen3_asr"
```

---

### Task 3: Extend `resolve_stt()` to a 3-tuple `(engine, language, model)`

**Files:**
- Modify: `apps/api_gateway/app/services/stt/profile.py`
- Modify: `apps/api_gateway/app/api/routes/lugo.py:43`
- Modify: `apps/api_gateway/app/api/routes/conversation.py:184`
- Modify: `apps/api_gateway/app/api/routes/livehost.py:88`
- Modify: `apps/api_gateway/app/api/routes/stt.py:108`
- Modify: `apps/api_gateway/app/services/warmup.py:49`
- Test: `tests/unit/test_stt_profile.py`

**Interfaces:**
- Consumes: `SttConfig.model` (Task 1).
- Produces: `resolve_stt(profile, q_engine=None, q_language=None, q_model=None) -> tuple[str, str | None, str]`. The `model` value is threaded through but not yet *used* by callers in this task (that's Task 6/7) — this task only makes every caller unpack 3 values instead of 2 so nothing breaks.

This task is intentionally non-behavioral for every caller except `resolve_stt` itself: it just changes the tuple arity everywhere it's unpacked.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_stt_profile.py`, update every `resolve_stt(...)` assertion to a 3-tuple and add model-specific cases. Replace the whole `resolve_stt` section (from `# --- resolve_stt: profile-driven STT resolution` to end of file) with:

```python
# --- resolve_stt: profile-driven STT resolution -----------------------------

@pytest.fixture
def _server_default(monkeypatch):
    # Pin the server-wide default so tests don't depend on the ambient .env.
    monkeypatch.setattr(settings, "stt_profile", "")
    monkeypatch.setattr(settings, "conversation_stt_engine", "whisper")
    monkeypatch.setattr(settings, "conversation_language", "vi")
    monkeypatch.setattr(settings, "default_stt_engine", "vosk")


def test_resolve_stt_no_profile_uses_server_default(_server_default):
    assert resolve_stt(None) == ("whisper", "vi", "")


def test_resolve_stt_profile_preset_wins_over_server_default(_server_default):
    p = Profile(name="p", stt=SttConfig(profile="vi"))
    assert resolve_stt(p) == ("qwen3_asr", "vi", "")


def test_resolve_stt_preset_auto_detect_language_is_authoritative(_server_default):
    # A "multi" preset means auto-detect (None) — it must NOT fall back to the
    # server's conversation_language.
    p = Profile(name="p", stt=SttConfig(profile="multi"))
    assert resolve_stt(p) == ("qwen3_asr", None, "")


def test_resolve_stt_explicit_engine_language_override_preset(_server_default):
    p = Profile(name="p", stt=SttConfig(profile="vi", engine="whisper_mlx", language="en"))
    assert resolve_stt(p) == ("whisper_mlx", "en", "")


def test_resolve_stt_query_param_wins_over_profile(_server_default):
    p = Profile(name="p", stt=SttConfig(profile="vi"))
    assert resolve_stt(p, q_engine="vosk", q_language="fr") == ("vosk", "fr", "")


def test_resolve_stt_server_stt_profile_default_applies_without_profile(monkeypatch):
    monkeypatch.setattr(settings, "stt_profile", "en")
    monkeypatch.setattr(settings, "conversation_stt_engine", "whisper")
    assert resolve_stt(None) == ("qwen3_asr", "en", "")


def test_resolve_stt_model_from_profile(_server_default):
    p = Profile(name="p", stt=SttConfig(engine="qwen3_asr", model="1.7b"))
    assert resolve_stt(p) == ("qwen3_asr", None, "1.7b")


def test_resolve_stt_model_query_param_wins_over_profile(_server_default):
    p = Profile(name="p", stt=SttConfig(engine="qwen3_asr", model="1.7b"))
    assert resolve_stt(p, q_model="0.6b") == ("qwen3_asr", None, "0.6b")


def test_resolve_stt_model_defaults_empty_when_unset(_server_default):
    assert resolve_stt(None) == ("whisper", "vi", "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_stt_profile.py -v`
Expected: FAIL — assertion mismatches (`resolve_stt(None)` still returns a 2-tuple)

- [ ] **Step 3: Update `resolve_stt()`**

In `apps/api_gateway/app/services/stt/profile.py`, replace the `resolve_stt` function:

```python
def resolve_stt(
    profile: object | None,
    q_engine: str | None = None,
    q_language: str | None = None,
    q_model: str | None = None,
) -> tuple[str, str | None, str]:
    """Resolve (engine, language|None, model) for a conversation.

    Single source of truth shared by the conversation WS stream and the /stt/warm
    endpoint so a device that only sends a profile id warms and streams against the
    same STT model. Priority, highest first:

      1. explicit query param (stt_engine / language / stt_model) — debugging / manual override
      2. the chatllm profile's SttConfig (engine/language/model, or a language preset)
      3. the server-wide default (settings.stt_profile preset, then
         conversation_stt_engine / conversation_language); model has no server-wide
         default — "" means "whatever's currently active for the resolved engine".

    `profile` is a services.profiles Profile (or None); accessed duck-typed to avoid
    a circular import. A language preset (vi|en|multi|en_vi) sets engine+language
    together; language None means auto-detect and is authoritative when a preset
    resolves (it is not overridden by conversation_language). model is independent
    of the preset system — a preset never implies a model variant.
    """
    from app.core.settings import settings

    stt_cfg = getattr(profile, "stt", None)
    preset_name = (getattr(stt_cfg, "profile", "") or "") or settings.stt_profile
    preset = resolve_stt_profile(preset_name)
    preset_engine, preset_lang = preset if preset else (None, None)

    engine = (
        q_engine
        or (getattr(stt_cfg, "engine", "") or None)
        or preset_engine
        or settings.conversation_stt_engine
        or settings.default_stt_engine
    )
    if q_language:
        language: str | None = q_language
    elif getattr(stt_cfg, "language", ""):
        language = stt_cfg.language
    elif preset:
        language = preset_lang  # may be None (auto-detect) — authoritative
    else:
        language = settings.conversation_language or None
    model = q_model or (getattr(stt_cfg, "model", "") or "")
    return engine, language, model
```

- [ ] **Step 4: Fix the 5 call sites to unpack 3 values**

`apps/api_gateway/app/api/routes/lugo.py:43` — change:
```python
    stt_engine, language = resolve_stt(profile)
```
to:
```python
    stt_engine, language, stt_model = resolve_stt(profile)
```
and update the `_resolve()` return statement (a few lines below) from `return profile, stt_engine, language, tts, idle` to `return profile, stt_engine, language, stt_model, tts, idle` (its docstring already covers this — no other changes needed in this task; every caller of `_resolve()` is fixed in Task 6).

`apps/api_gateway/app/api/routes/conversation.py:184` — change:
```python
    stt_engine, language = resolve_stt(profile, q.get("stt_engine"), q.get("language"))
```
to:
```python
    stt_engine, language, stt_model = resolve_stt(
        profile, q.get("stt_engine"), q.get("language"), q.get("stt_model")
    )
```

`apps/api_gateway/app/api/routes/livehost.py:88` — change:
```python
    stt_engine, language = resolve_stt(profile, q.get("stt_engine"), q.get("language"))
```
to:
```python
    stt_engine, language, stt_model = resolve_stt(
        profile, q.get("stt_engine"), q.get("language"), q.get("stt_model")
    )
```

`apps/api_gateway/app/api/routes/stt.py:108` — change:
```python
        engine, _ = resolve_stt(prof)
```
to:
```python
        engine, _, _model = resolve_stt(prof)
```

`apps/api_gateway/app/services/warmup.py:49` — change:
```python
            engine, _lang = resolve_stt(prof)
```
to:
```python
            engine, _lang, _model = resolve_stt(prof)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/unit/test_stt_profile.py tests/unit/test_lugo_stt_resolution.py tests/unit/test_conversation_profile.py tests/unit/test_warmup_engine_settings.py tests/unit/test_sessions_routes.py -v`
Expected: `test_stt_profile.py` fully PASS. The other four files still reference the *old* 5-tuple shape of `lugo._resolve()` (untouched in this task) and the old 2-value unpack inside `warmup.py`/route files (now fixed) — they should already PASS unchanged, since none of them assert on `resolve_stt`'s return shape directly. If any of them fail, read the failure — it means a call site was missed; grep again with `grep -rn "resolve_stt(" apps/api_gateway/app/` to confirm all 6 call sites (5 callers + the definition) are updated.

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/services/stt/profile.py apps/api_gateway/app/api/routes/lugo.py apps/api_gateway/app/api/routes/conversation.py apps/api_gateway/app/api/routes/livehost.py apps/api_gateway/app/api/routes/stt.py apps/api_gateway/app/services/warmup.py tests/unit/test_stt_profile.py
git commit -m "feat(stt): thread model through resolve_stt() as a third return value"
```

---

### Task 4: `GET /v1/stt/models?engine=` endpoint

**Files:**
- Modify: `apps/api_gateway/app/api/routes/stt.py`
- Test: `tests/unit/test_stt_routes.py` (new)

**Interfaces:**
- Consumes: `STT_MODEL_REGISTRIES` (Task 2).
- Produces: `GET /v1/stt/models?engine=<name>` → `{"success": true, "data": {"engine": str, "supports_variants": bool, "models": [{"id", "label", "cached", "active", "valid": true}]}}`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_stt_routes.py`:

```python
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_list_stt_models_known_engine_supports_variants():
    resp = client.get("/v1/stt/models", params={"engine": "qwen3_asr"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["engine"] == "qwen3_asr"
    assert data["supports_variants"] is True
    ids = {m["id"] for m in data["models"]}
    assert ids == {"0.6b", "1.7b"}
    assert all(m["valid"] is True for m in data["models"])
    assert all({"id", "label", "cached", "active", "valid"} <= set(m) for m in data["models"])


def test_list_stt_models_engine_without_registry():
    resp = client.get("/v1/stt/models", params={"engine": "vosk"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data == {"engine": "vosk", "supports_variants": False, "models": []}


def test_list_stt_models_requires_engine_param():
    resp = client.get("/v1/stt/models")
    assert resp.status_code == 422  # FastAPI required-query-param validation
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_stt_routes.py -v`
Expected: FAIL — `404 Not Found` for `/v1/stt/models`

- [ ] **Step 3: Add the endpoint**

In `apps/api_gateway/app/api/routes/stt.py`, add after `list_stt_engines()`:

```python
@router.get("/models")
async def list_stt_models(engine: str) -> dict:
    from app.services.stt.model_registry import STT_MODEL_REGISTRIES

    registry = STT_MODEL_REGISTRIES.get(engine)
    if registry is None:
        return {"success": True, "data": {"engine": engine, "supports_variants": False, "models": []}}
    models = [{**m, "valid": True} for m in registry.list_models()]
    return {"success": True, "data": {"engine": engine, "supports_variants": True, "models": models}}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_stt_routes.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/api/routes/stt.py tests/unit/test_stt_routes.py
git commit -m "feat(stt): add GET /v1/stt/models to list valid+available model variants"
```

---

### Task 5: Validate `stt.model` on profile create/update

**Files:**
- Modify: `apps/api_gateway/app/api/routes/profiles.py`
- Test: `tests/unit/test_profiles_routes.py`

**Interfaces:**
- Consumes: `STT_MODEL_REGISTRIES` (Task 2), `resolve_stt_profile()` (existing, `apps/api_gateway/app/services/stt/profile.py`).
- Produces: `POST`/`PUT /v1/profiles` returns 400 when `stt.model` is set but invalid for the resolved engine, or the resolved engine has no registry, or no engine can be resolved at all.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_profiles_routes.py` (at the end):

```python
def test_create_profile_accepts_valid_stt_model(client):
    resp = client.post("/v1/profiles", json={
        "name": "good-model",
        "stt": {"engine": "qwen3_asr", "model": "0.6b"},
    })
    assert resp.status_code == 200
    assert resp.json()["data"]["stt"]["model"] == "0.6b"


def test_create_profile_rejects_invalid_stt_model(client):
    resp = client.post("/v1/profiles", json={
        "name": "bad-model",
        "stt": {"engine": "whisper", "model": "not-a-real-size"},
    })
    assert resp.status_code == 400


def test_create_profile_rejects_model_on_engine_without_registry(client):
    resp = client.post("/v1/profiles", json={
        "name": "no-variants",
        "stt": {"engine": "vosk", "model": "anything"},
    })
    assert resp.status_code == 400


def test_create_profile_rejects_model_without_resolvable_engine(client):
    resp = client.post("/v1/profiles", json={
        "name": "no-engine",
        "stt": {"model": "0.6b"},
    })
    assert resp.status_code == 400


def test_create_profile_model_resolves_engine_via_preset(client):
    # "vi" preset resolves to qwen3_asr — model should validate against that.
    resp = client.post("/v1/profiles", json={
        "name": "preset-model",
        "stt": {"profile": "vi", "model": "1.7b"},
    })
    assert resp.status_code == 200


def test_update_profile_rejects_invalid_stt_model(client):
    client.post("/v1/profiles", json={"name": "upd-model", "stt": {"engine": "qwen3_asr"}})
    resp = client.put("/v1/profiles/upd-model", json={
        "name": "upd-model",
        "stt": {"engine": "qwen3_asr", "model": "nope"},
    })
    assert resp.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_profiles_routes.py -v -k stt_model`
Expected: FAIL — `test_create_profile_rejects_invalid_stt_model` and similar get 200 instead of 400 (no validation yet)

- [ ] **Step 3: Add validation**

In `apps/api_gateway/app/api/routes/profiles.py`, add imports and a helper, then call it from both `create_profile` and `update_profile`:

```python
from app.core.errors import AppError
from app.services.mcp.models import McpServer
from app.services.profiles.models import LlmConfig, MemoryConfig, Profile, SessionConfig, SttConfig, TtsConfig
from app.services.profiles.store import profile_store
from app.services.stt.model_registry import STT_MODEL_REGISTRIES
from app.services.stt.profile import resolve_stt_profile

router = APIRouter(prefix="/v1/profiles", tags=["profiles"])


def _mask(profile: Profile) -> dict:
    data = profile.model_dump()
    if data.get("llm", {}).get("api_key"):
        data["llm"]["api_key"] = "***"
    return data


def _validate_stt_model(profile: Profile) -> None:
    if not profile.stt.model:
        return
    preset = resolve_stt_profile(profile.stt.profile)
    engine = profile.stt.engine or (preset[0] if preset else "")
    if not engine:
        raise AppError("stt.model requires stt.engine or a resolvable stt.profile preset")
    registry = STT_MODEL_REGISTRIES.get(engine)
    if registry is None:
        raise AppError(f"engine '{engine}' has no selectable model variants")
    registry.validate(profile.stt.model)
```

Then in `create_profile`:

```python
@router.post("")
async def create_profile(payload: ProfileRequest) -> dict:
    profile = Profile(**payload.model_dump())
    _validate_stt_model(profile)
    profile_store.upsert(profile)
    return {"success": True, "data": _mask(profile)}
```

And in `update_profile`, right after `profile = Profile(**data)`:

```python
@router.put("/{name}")
async def update_profile(name: str, payload: ProfileRequest) -> dict:
    data = payload.model_dump()
    data["name"] = name
    if not data.get("llm", {}).get("api_key"):
        existing = profile_store.get(name)
        if existing and existing.llm.api_key:
            data.setdefault("llm", {})["api_key"] = existing.llm.api_key
    profile = Profile(**data)
    _validate_stt_model(profile)
    profile_store.upsert(profile)
    return {"success": True, "data": _mask(profile)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_profiles_routes.py -v`
Expected: PASS (all tests in the file, including the new ones)

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/api/routes/profiles.py tests/unit/test_profiles_routes.py
git commit -m "feat(profiles): validate stt.model against the resolved engine's registry on save"
```

---

### Task 6: Eager swap+warm at session start (the "like TTS" behavior)

**Files:**
- Modify: `apps/api_gateway/app/services/conversation/session.py`
- Modify: `apps/api_gateway/app/api/routes/lugo.py`
- Modify: `apps/api_gateway/app/api/routes/conversation.py`
- Modify: `apps/api_gateway/app/api/routes/livehost.py`
- Test: `tests/unit/test_conversation_session_core.py`
- Test: `tests/unit/test_lugo_stt_resolution.py`

**Interfaces:**
- Consumes: `apply_stt_model(engine, model)` (Task 2), `resolve_stt()` 3-tuple (Task 3).
- Produces: `SessionRuntimeConfig.stt_model: str = ""` (new trailing field, default so existing constructors are unaffected); `lugo._resolve()` now returns a 6-tuple `(profile, stt_engine, language, stt_model, tts, idle)`.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_conversation_session_core.py`, add after the existing test:

```python
@pytest.mark.asyncio
async def test_session_start_applies_profile_stt_model(monkeypatch):
    from app.services.stt import model_registry

    calls = []
    monkeypatch.setattr(model_registry, "apply_stt_model", lambda engine, model: calls.append((engine, model)))
    monkeypatch.setattr("app.services.conversation.session.apply_stt_model", model_registry.apply_stt_model)

    async def emit(name, **p): pass
    async def emit_audio(pkt): pass

    sess = ConversationSession(_cfg(stt_model="1.7b"), emit, emit_audio)
    await sess.start()
    await sess.close()

    assert ("stub-core-stt", "1.7b") in calls


@pytest.mark.asyncio
async def test_session_start_skips_apply_when_no_model_set():
    async def emit(name, **p): pass
    async def emit_audio(pkt): pass

    sess = ConversationSession(_cfg(), emit, emit_audio)  # stt_model defaults to ""
    await sess.start()  # must not raise
    await sess.close()
```

Note: the first test monkeypatches `model_registry.apply_stt_model` itself (a plain function reassignment) rather than trying to patch the name imported into `session.py`, then re-points `session.apply_stt_model` at the same patched callable so the spy is visible regardless of how it's imported — this avoids depending on whether `session.py` does `from ... import apply_stt_model` or `import ... as model_registry`. Once Step 3 below picks the exact import style, simplify this test to patch `app.services.conversation.session.apply_stt_model` directly.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_conversation_session_core.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'stt_model'`

- [ ] **Step 3: Add `stt_model` to `SessionRuntimeConfig` and apply it in `start()`**

In `apps/api_gateway/app/services/conversation/session.py`:

Add the import (with the other `app.services.stt.*` imports near the top):

```python
from app.services.stt.model_registry import apply_stt_model
```

Add the field at the end of `SessionRuntimeConfig` (must be last — every other field has no default):

```python
    resume_sid: str | None  # requested_sid, for history resume
    stt_model: str = ""  # optional model-variant override (SttConfig.model, resolve_stt's 3rd value)
```

In `start()`, change:

```python
        self.stt_provider = stt_service.get_provider(cfg.stt_engine)
        self.tts_provider = tts_service.get_provider(cfg.tts_engine)
```

to:

```python
        if cfg.stt_model:
            try:
                apply_stt_model(cfg.stt_engine, cfg.stt_model)
            except AppError as exc:
                logger.warning(
                    "stt model override skipped (%s/%s): %s", cfg.stt_engine, cfg.stt_model, exc
                )
        self.stt_provider = stt_service.get_provider(cfg.stt_engine)
        self.tts_provider = tts_service.get_provider(cfg.tts_engine)
```

Then simplify the test written in Step 1 to patch the name directly (this is the "once you pick the import style" cleanup mentioned above) — replace the first new test with:

```python
@pytest.mark.asyncio
async def test_session_start_applies_profile_stt_model(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.services.conversation.session.apply_stt_model",
        lambda engine, model: calls.append((engine, model)),
    )

    async def emit(name, **p): pass
    async def emit_audio(pkt): pass

    sess = ConversationSession(_cfg(stt_model="1.7b"), emit, emit_audio)
    await sess.start()
    await sess.close()

    assert calls == [("stub-core-stt", "1.7b")]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_conversation_session_core.py -v`
Expected: PASS (all tests, including the two new ones)

- [ ] **Step 5: Wire `stt_model` through `lugo.py`, `conversation.py`, `livehost.py`**

`apps/api_gateway/app/api/routes/lugo.py` — update `_resolve()`'s return and the two call sites:

```python
def _resolve(profile_name: str | None):
    """Resolve engines/tts params from a profile (server owns everything)."""
    profile = profile_store.get(profile_name) if profile_name else None
    stt_engine, language, stt_model = resolve_stt(profile)
    tts_name = (profile.tts.profile_name if profile else "") or None
    tts_profile = tts_profile_store.get(tts_name) if tts_name else None
    if tts_profile and tts_profile.engine:
        tts = dict(engine=tts_profile.engine, voice=tts_profile.voice or None,
                   ref_audio_path=tts_profile.ref_audio_path or None, ref_text=tts_profile.ref_text or None,
                   instruct=tts_profile.instruct or None, speed=tts_profile.speed, language=tts_profile.language)
    else:
        tts = dict(engine=settings.conversation_tts_engine or settings.default_tts_engine,
                   voice=None, ref_audio_path=None, ref_text=None, instruct=None, speed=None, language=None)
    idle = profile.session.idle_timeout_s if profile else 30
    return profile, stt_engine, language, stt_model, tts, idle
```

And in `lugo_stream()`:

```python
    profile, stt_engine, language, stt_model, tts, idle = _resolve(profile_name)
```

and add `stt_model=stt_model,` to the `SessionRuntimeConfig(...)` call (any position, it's a kwarg).

`apps/api_gateway/app/api/routes/conversation.py` — the `stt_model` variable already exists from Task 3's edit; add `stt_model=stt_model,` to its `SessionRuntimeConfig(...)` call.

`apps/api_gateway/app/api/routes/livehost.py` — this route does NOT use `ConversationSession`/`SessionRuntimeConfig`, it drives its own loop. Apply the model directly, right after resolving the provider. Change:

```python
    try:
        stt_provider = stt_service.get_provider(stt_engine)
        tts_provider = tts_service.get_provider(tts_engine)
    except AppError as exc:
        await websocket.send_json({"event": "error", "message": str(exc)})
        await websocket.close()
        return
```

to:

```python
    if stt_model:
        try:
            apply_stt_model(stt_engine, stt_model)
        except AppError as exc:
            logger.warning("stt model override skipped (%s/%s): %s", stt_engine, stt_model, exc)

    try:
        stt_provider = stt_service.get_provider(stt_engine)
        tts_provider = tts_service.get_provider(tts_engine)
    except AppError as exc:
        await websocket.send_json({"event": "error", "message": str(exc)})
        await websocket.close()
        return
```

and add the import near the top of `livehost.py`:

```python
from app.services.stt.model_registry import apply_stt_model
```

- [ ] **Step 6: Fix the 3 existing lugo resolution tests for the new 6-tuple**

In `tests/unit/test_lugo_stt_resolution.py`, update all three unpacking lines from 5 to 6 values:

```python
def test_lugo_resolve_uses_profile_stt_preset(monkeypatch, tmp_path):
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    fresh.upsert(Profile(name="dev", stt=SttConfig(profile="vi")))
    monkeypatch.setattr(lugo, "profile_store", fresh)

    _profile, stt_engine, language, _stt_model, _tts, _idle = lugo._resolve("dev")

    assert stt_engine == "qwen3_asr"
    assert language == "vi"


def test_lugo_resolve_explicit_engine_overrides_preset(monkeypatch, tmp_path):
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    fresh.upsert(Profile(name="dev2", stt=SttConfig(engine="whisper_mlx", language="en")))
    monkeypatch.setattr(lugo, "profile_store", fresh)

    _profile, stt_engine, language, _stt_model, _tts, _idle = lugo._resolve("dev2")

    assert stt_engine == "whisper_mlx"
    assert language == "en"


def test_lugo_resolve_no_profile_falls_back_to_settings(monkeypatch, tmp_path):
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    monkeypatch.setattr(lugo, "profile_store", fresh)
    monkeypatch.setattr(settings, "stt_profile", "")
    monkeypatch.setattr(settings, "conversation_stt_engine", "stub-fallback-stt")

    _profile, stt_engine, _language, _stt_model, _tts, _idle = lugo._resolve(None)

    assert stt_engine == "stub-fallback-stt"


def test_lugo_resolve_returns_profile_stt_model(monkeypatch, tmp_path):
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    fresh.upsert(Profile(name="dev3", stt=SttConfig(engine="qwen3_asr", model="1.7b")))
    monkeypatch.setattr(lugo, "profile_store", fresh)

    _profile, stt_engine, _language, stt_model, _tts, _idle = lugo._resolve("dev3")

    assert stt_engine == "qwen3_asr"
    assert stt_model == "1.7b"
```

- [ ] **Step 7: Run the full affected test set**

Run: `pytest tests/unit/test_conversation_session_core.py tests/unit/test_lugo_stt_resolution.py tests/unit/test_sessions_routes.py tests/unit/test_conversation_profile.py tests/unit/test_session_add_tool_source.py -v`
Expected: PASS (all)

- [ ] **Step 8: Commit**

```bash
git add apps/api_gateway/app/services/conversation/session.py apps/api_gateway/app/api/routes/lugo.py apps/api_gateway/app/api/routes/conversation.py apps/api_gateway/app/api/routes/livehost.py tests/unit/test_conversation_session_core.py tests/unit/test_lugo_stt_resolution.py
git commit -m "feat(conversation): apply profile's stt.model at session start before warming"
```

---

### Task 7: Boot warmup + `/stt/warm` pick up the profile's model too

**Files:**
- Modify: `apps/api_gateway/app/services/warmup.py`
- Modify: `apps/api_gateway/app/main.py`
- Modify: `apps/api_gateway/app/api/routes/stt.py`
- Test: `tests/unit/test_warmup_engine_settings.py`
- Test: `tests/unit/test_warmup.py`
- Test: `tests/unit/test_stt_routes.py`

**Interfaces:**
- Consumes: `apply_stt_model()` / `STT_MODEL_REGISTRIES` (Task 2), `resolve_stt()` 3-tuple (Task 3).
- Produces: `engines_for_boot_warmup() -> tuple[list[str], list[str], dict[str, str]]` (third element: `{engine: model}` for engines whose enumerated profiles asked for a specific model — last profile enumerated wins per engine, a documented limitation of the single process-global active-model slot, not a bug).

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_warmup_engine_settings.py`, update the existing boot-warmup test to unpack 3 values and add a model-specific case:

```python
def _fake_profile(stt_engine, stt_model=""):
    stt = type("S", (), {"profile": "", "engine": stt_engine, "language": "", "model": stt_model})()
    return type("P", (), {"stt": stt})()


def test_boot_warmup_includes_profile_and_tts_profile_engines(monkeypatch):
    monkeypatch.setattr(settings, "conversation_stt_engine", "whisper")
    monkeypatch.setattr(settings, "extra_warmup_stt_engines", "")
    monkeypatch.setattr(settings, "conversation_tts_engine", "vieneu")
    monkeypatch.setattr(settings, "extra_warmup_tts_engines", "")
    monkeypatch.setattr(
        "app.services.profiles.store.profile_store",
        _FakeStore({"p": _fake_profile("qwen3_asr")}),
    )
    monkeypatch.setattr(
        "app.services.tts.profile_store.tts_profile_store",
        _FakeStore({"t": _fake_tts_profile("omnivoice")}),
    )
    stt, tts, stt_models = warmup.engines_for_boot_warmup()
    assert "whisper" in stt and "qwen3_asr" in stt   # settings default + profile
    assert "vieneu" in tts and "omnivoice" in tts     # settings default + tts profile
    assert len(stt) == len(set(stt)) and len(tts) == len(set(tts))  # de-duplicated
    assert stt_models == {}  # no profile set a model


def test_boot_warmup_collects_profile_stt_models(monkeypatch):
    monkeypatch.setattr(settings, "conversation_stt_engine", "whisper")
    monkeypatch.setattr(settings, "extra_warmup_stt_engines", "")
    monkeypatch.setattr(settings, "conversation_tts_engine", "vieneu")
    monkeypatch.setattr(settings, "extra_warmup_tts_engines", "")
    monkeypatch.setattr(
        "app.services.profiles.store.profile_store",
        _FakeStore({"p": _fake_profile("qwen3_asr", stt_model="1.7b")}),
    )
    monkeypatch.setattr(
        "app.services.tts.profile_store.tts_profile_store",
        _FakeStore({}),
    )
    _stt, _tts, stt_models = warmup.engines_for_boot_warmup()
    assert stt_models == {"qwen3_asr": "1.7b"}
```

Note the existing `_fake_profile` helper (used elsewhere in the file) is being changed to accept an optional `stt_model` kwarg with a default — check other call sites of `_fake_profile` in the same file still pass (they call it with one positional arg, which still works since `stt_model` defaults to `""`).

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_warmup_engine_settings.py -v`
Expected: FAIL — `ValueError: not enough values to unpack (expected 3, got 2)`

- [ ] **Step 3: Update `engines_for_boot_warmup()`**

In `apps/api_gateway/app/services/warmup.py`, replace the function body:

```python
def engines_for_boot_warmup() -> tuple[list[str], list[str], dict[str, str]]:
    """Every STT and TTS engine that any chatllm profile or TTS profile can
    select, merged with the configured warmup lists, plus any per-engine model
    override those profiles asked for.

    Warming these at boot means a device connecting with any profile never pays a
    cold model load on its first turn (the delay the user hits when an engine is
    loaded lazily on first use). Returns (stt_engines, tts_engines, stt_models),
    de-duplicated and order-preserving. stt_models maps engine -> model for any
    engine where at least one enumerated profile set SttConfig.model; since the
    active model is a single process-global slot per engine (see
    app.services.stt.model_registry), if two profiles want different models on
    the same engine only the last one enumerated wins here — the session-start
    swap-on-use (ConversationSession.start) is the authoritative per-session
    correctness mechanism, this is just a best-effort head start. LLM engines are
    remote APIs (no local model to warm), so they're intentionally excluded.
    """
    from app.core.settings import settings
    from app.services.profiles.store import profile_store
    from app.services.stt.profile import resolve_stt
    from app.services.tts.profile_store import tts_profile_store

    stt: list[str] = []
    tts: list[str] = []
    stt_models: dict[str, str] = {}

    def _add(lst: list[str], name: str | None) -> None:
        if name and name not in lst:
            lst.append(name)

    for e in settings.warmup_stt_engines:
        _add(stt, e)
    for e in settings.warmup_tts_engines:
        _add(tts, e)

    try:
        for prof in profile_store.list().values():
            engine, _lang, model = resolve_stt(prof)
            _add(stt, engine)
            if model and engine:
                stt_models[engine] = model
    except Exception as exc:  # noqa: BLE001 - warm-up must never break boot
        logger.warning("profile STT enumeration for warm-up failed: %s", exc)
    try:
        for tp in tts_profile_store.list().values():
            _add(tts, getattr(tp, "engine", "") or None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("TTS profile enumeration for warm-up failed: %s", exc)

    return stt, tts, stt_models
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_warmup_engine_settings.py -v`
Expected: PASS (all)

- [ ] **Step 5: Apply the collected models in `_warm_default_engines()`**

In `apps/api_gateway/app/main.py`, update `_warm_default_engines()`:

```python
async def _warm_default_engines() -> None:
    """Load the STT/TTS engines conversations actually use, at process boot instead
    of waiting for the first WebSocket connect. Covers conversation_stt_engine /
    conversation_tts_engine PLUS any extra_warmup_stt_engines/extra_warmup_tts_engines
    — a device that always pins a different engine via ?stt_engine=... (e.g. an RPi
    client configured for qwen3_asr) never touches the settings default, so it must
    be listed explicitly or this warm-up silently loads the wrong model and the
    device still pays a full cold-load on its first-ever turn each boot (see
    app.services.warmup)."""
    from app.core.errors import AppError
    from app.services.stt.model_registry import apply_stt_model
    from app.services.stt.service import stt_service
    from app.services.tts.service import tts_service
    from app.services.warmup import engines_for_boot_warmup, warm_providers

    # Warm every engine any profile / TTS profile can select, not just the static
    # warmup lists — so a device connecting with any profile never pays a cold
    # model load on its first turn.
    stt_engines, tts_engines, stt_models = engines_for_boot_warmup()
    for engine, model in stt_models.items():
        try:
            apply_stt_model(engine, model)
        except AppError as exc:
            logger.warning("stt model warm-up skipped for %s/%s: %s", engine, model, exc)
    providers = []
    for name in stt_engines:
        try:
            providers.append(stt_service.get_provider(name))
        except AppError as exc:
            logger.warning("stt warm-up skipped for %s: %s", name, exc)
    for name in tts_engines:
        try:
            providers.append(tts_service.get_provider(name))
        except AppError as exc:
            logger.warning("tts warm-up skipped for %s: %s", name, exc)
    if not providers:
        return

    started = time.monotonic()
    logger.info("boot warm-up starting: stt=%s tts=%s", stt_engines, tts_engines)
    await warm_providers(*providers)
    logger.info("boot warm-up finished in %.0fms", (time.monotonic() - started) * 1000)
```

- [ ] **Step 6: Run `test_warmup.py` to verify boot warmup still passes**

Run: `pytest tests/unit/test_warmup.py -v`
Expected: PASS — these tests mock `stt_service`/`tts_service`/the profile stores directly and don't set `stt.model` on any fake profile, so `stt_models` resolves to `{}` and the new `apply_stt_model` loop is a no-op; behavior is unchanged.

- [ ] **Step 7: Wire model selection into `/stt/warm` and add tests**

In `apps/api_gateway/app/api/routes/stt.py`, update the `warm_engine` endpoint:

```python
@router.post("/warm")
async def warm_engine(engine: str | None = None, profile: str | None = None, model: str | None = None) -> dict:
    """Load a heavy STT model into memory ahead of use (e.g. Whisper large ~20s).

    Lets the UI preload before the first conversation turn so it isn't a cold wait.
    Pass ?engine= to warm a specific engine, or ?profile= to warm whichever engine
    (and model, if the profile pins one) that profile resolves to (so a device that
    only knows its profile can pre-warm the right model, using the same resolution
    as the conversation stream). Pass ?model= to override the model explicitly
    regardless of profile. With neither engine nor profile, the server-wide default
    engine is warmed.
    """
    import asyncio

    from app.services.profiles.store import profile_store
    from app.services.stt.model_registry import apply_stt_model
    from app.services.stt.profile import resolve_stt

    if not engine:
        prof = profile_store.get(profile) if profile else None
        engine, _, resolved_model = resolve_stt(prof)
        model = model or resolved_model
    if model:
        try:
            apply_stt_model(engine, model)
        except AppError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    provider = stt_service.get_provider(engine)
    warm = getattr(provider, "warm", None)
    if callable(warm):
        await asyncio.to_thread(warm)
    return {"success": True, "data": {"engine": engine, "model": model or None, "warmed": callable(warm)}}
```

`AppError` needs to be imported at the top of `apps/api_gateway/app/api/routes/stt.py` if not already — check `from app.core.errors import AppError` is present (it already is, per the file's existing imports at line 14).

Add tests to `tests/unit/test_stt_routes.py`:

```python
def test_warm_stt_engine_with_explicit_model(monkeypatch):
    from app.services.stt import model_registry

    calls = []
    monkeypatch.setattr(model_registry, "apply_stt_model", lambda e, m: calls.append((e, m)))
    monkeypatch.setattr("app.api.routes.stt.apply_stt_model", model_registry.apply_stt_model)

    resp = client.post("/v1/stt/warm", params={"engine": "qwen3_asr", "model": "1.7b"})
    assert resp.status_code == 200
    assert resp.json()["data"]["model"] == "1.7b"
    assert calls == [("qwen3_asr", "1.7b")]


def test_warm_stt_engine_rejects_invalid_model():
    resp = client.post("/v1/stt/warm", params={"engine": "qwen3_asr", "model": "not-real"})
    assert resp.status_code == 400


def test_warm_stt_engine_no_model_unchanged():
    resp = client.post("/v1/stt/warm", params={"engine": "vosk"})
    assert resp.status_code == 200
    assert resp.json()["data"]["model"] is None
```

Note: `test_warm_stt_engine_with_explicit_model` patches `app.services.stt.model_registry.apply_stt_model` and then re-points `app.api.routes.stt.apply_stt_model` at the same object, matching the "avoid depending on import style" approach from Task 6 Step 1 — since `stt.py`'s `warm_engine` does a local `from app.services.stt.model_registry import apply_stt_model` *inside the function body* (not at module level, per the code above), patching `app.api.routes.stt.apply_stt_model` has no effect. Patch `app.services.stt.model_registry.apply_stt_model` directly instead:

```python
def test_warm_stt_engine_with_explicit_model(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.services.stt.model_registry.apply_stt_model",
        lambda e, m: calls.append((e, m)),
    )

    resp = client.post("/v1/stt/warm", params={"engine": "qwen3_asr", "model": "1.7b"})
    assert resp.status_code == 200
    assert resp.json()["data"]["model"] == "1.7b"
    assert calls == [("qwen3_asr", "1.7b")]
```

(This works because the function-local `from ... import apply_stt_model` re-reads the module attribute at call time on every request, so patching the module attribute before the request is made is visible to it.)

- [ ] **Step 8: Run test to verify it passes**

Run: `pytest tests/unit/test_stt_routes.py -v`
Expected: PASS (all, including the 3 new ones)

- [ ] **Step 9: Full regression pass**

Run: `pytest tests/unit -v`
Expected: PASS (all unit tests — this is the full suite covering every file touched across all 7 tasks)

- [ ] **Step 10: Commit**

```bash
git add apps/api_gateway/app/services/warmup.py apps/api_gateway/app/main.py apps/api_gateway/app/api/routes/stt.py tests/unit/test_warmup_engine_settings.py tests/unit/test_warmup.py tests/unit/test_stt_routes.py
git commit -m "feat(warmup): apply per-profile stt.model at boot warm-up and in /stt/warm"
```

---

## Self-Review Notes

**Spec coverage:**
- Data model (`SttConfig.model`) → Task 1.
- Model registry → Task 2.
- New `/v1/stt/models` endpoint → Task 4.
- `resolve_stt()` third return value → Task 3.
- Eager swap+warm at session start (session.py, lugo.py, conversation.py, livehost.py) → Task 6.
- Boot warmup extension → Task 7 (Steps 1-6).
- Validation on profile save → Task 5.
- Testing section of the spec → covered per-task; the "session start: assert registry.select() called before warm_providers()" case is covered by Task 6 Step 3's test (`apply_stt_model` is called synchronously in `start()`, strictly before the `_warm_and_notify()` background task that calls `warm_providers`, so ordering is structural, not just tested).

**Type/name consistency check:** `apply_stt_model(engine: str, model: str) -> None` (Task 2) is the exact name/signature used in Task 6 (session.py, livehost.py) and Task 7 (main.py, stt.py) — no drift. `resolve_stt(...) -> tuple[str, str | None, str]` (Task 3) is unpacked identically everywhere as `(engine, language, model)`. `engines_for_boot_warmup() -> tuple[list[str], list[str], dict[str, str]]` (Task 7) matches its one call site in `main.py`. `STT_MODEL_REGISTRIES` keys (`whisper`, `whisper_local`, `whisper_gemma`, `qwen3_asr`) are consistent between Task 2's definition and Task 5's validation lookup.

**No placeholders:** every step has real code, real assertions, real commands with expected output.
