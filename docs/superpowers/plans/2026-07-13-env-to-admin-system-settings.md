# Move runtime settings from .env to Admin System Settings — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move ~70 STT/TTS/LLM/conversation-tuning/preprocessing fields out of `Settings`/`.env` into the SQLite-backed `system_config_store`, editable at runtime via the existing `/v1/system/config` admin panel, with cache invalidation so cached-at-boot singletons pick up admin edits without a restart.

**Architecture:** `SystemConfig` (in `apps/api_gateway/app/services/system_config.py`) grows 7 nested Pydantic sub-models (one row, JSON blob, no schema migration). `PUT /v1/system/config` diffs old vs. new config and calls 4 targeted cache-invalidation hooks. Every consumer call site across the codebase is rewired from `settings.<field>` to `system_config_store.get().<group>.<field>`, then the field is deleted from `Settings`/`.env`.

**Tech Stack:** FastAPI, Pydantic v2, SQLAlchemy (sync engine), pytest, vanilla JS (no framework) for the admin SPA.

**Spec:** `docs/superpowers/specs/2026-07-13-env-to-admin-system-settings-design.md` — read it first; this plan implements it exactly, using field lists/defaults verified directly against the current `apps/api_gateway/app/core/settings.py`.

## Global Constraints

- No import path from old `.env` values into the DB (spec decision 2) — new `SystemConfig` fields use hard-coded Pydantic defaults copied verbatim from `settings.py`, nothing reads the `.env` file for these fields ever again.
- 5 secret fields get mask-on-GET / preserve-on-blank-or-`***`-PUT: `openrouter_api_key`, `conversation_llm.conversation_llm_api_key`, `remote_stt.whisper_service_api_key`, `remote_stt.eventlab_api_key`, `preprocessing.pyannote_auth_token`.
- Only 4 cache-invalidation hooks exist (spec's Cache invalidation table): `reinit_remote_providers` (remote_stt group), `clear_pyannote_cache` (preprocessing.pyannote_* fields), `clear_qwen3_asr_model_cache` (stt_local.qwen3_asr_device), `reset_voice_ref_and_respawn` (omnivoice group, any field). Every other group is already live-read per call — no hook needed.
- Existing runtime-override precedence must be preserved exactly: `_active_model`/`set_active_whisper_model()` (whisper), `_active_path`/`set_active_vosk_path()` (vosk), `_active_model`/`set_active_qwen3_asr_model()` (qwen3_asr), `_active_model`/`set_active_omnivoice_model()` (omnivoice) all short-circuit BEFORE falling back to config. The fallback target changes from `settings.X` to `system_config_store.get().group.X`; the override-wins-first behavior does not change.
- **Per-file granularity for consumer-rewiring tasks (Tasks 2–7):** given ~70 fields spread across 20+ files, each step in those tasks rewires one file's settings reads for the fields in that task's group, with a test-verification step per file, rather than one full red/green TDD cycle per individual field. Task 1 (foundational, additive-only) and the cache-invalidation hooks (Tasks 6/7) DO use full field/behavior-level TDD since they're genuinely new logic, not mechanical rewiring.
- **Files touched by more than one task:** `profile.py` (`resolve_stt`), `session.py`, `stt.py` (routes), `system.py` (routes), `conversation.py` (routes), `livehost.py`, `lugo.py` are each edited in multiple tasks (once per settings group they read). Every step below shows the FULL current line(s) and the FULL new line(s) — including any sibling fields NOT yet migrated (still reading `settings.X` because their task hasn't run yet) — so a partial-file diff is never ambiguous about what to touch and what to leave alone.
- Full existing test suite must stay green after every task before moving to the next (repo convention — see project memory `test-before-push-deploy`).

---

### Task 1: SystemConfig data model, store, API, and UI scaffold

**Files:**
- Modify: `apps/api_gateway/app/services/system_config.py`
- Modify: `apps/api_gateway/app/api/routes/system.py`
- Modify: `apps/api_gateway/app/static/js/base-context.js` → renamed to `apps/api_gateway/app/static/js/system-config.js`
- Modify: `apps/api_gateway/app/static/js/conversation.js`, `stt-batch.js`, `stt-stream.js`, `chat.js`, `livehost.js` (each has `import { getPreproc } from "./base-context.js"` — update the import path)
- Modify: `apps/api_gateway/app/static/index.html` (system config panel markup)
- Modify: `tests/unit/test_system_config_store.py`, `tests/unit/test_system_config_routes.py`
- Test: same two files above

**Interfaces:**
- Produces: `SystemConfig` gains 7 nested models — `EngineDefaults`, `SttLocalConfig`, `OmnivoiceConfig`, `ConversationLlmConfig`, `RemoteSttConfig`, `ConversationTuningConfig`, `PreprocessingConfig` — as fields `engines`, `stt_local`, `omnivoice`, `conversation_llm`, `remote_stt`, `conversation`, `preprocessing` on `SystemConfig`. `SystemConfigStore.set(config: SystemConfig) -> SystemConfig` (new generic full-replace method, used by every later task's route/hook code). Nothing consumes these fields yet — Tasks 2–7 do that.

- [ ] **Step 1: Write failing tests for the 7 new nested groups' defaults and the generic `set()` method**

Append to `tests/unit/test_system_config_store.py`:

```python
def test_engine_defaults_have_expected_defaults(tmp_path):
    s = SystemConfigStore(str(tmp_path / "system_config.json"))
    e = s.get().engines
    assert e.default_stt_engine == "vosk"
    assert e.default_tts_engine == "omnivoice"
    assert e.extra_warmup_stt_engines == ""
    assert e.extra_warmup_tts_engines == ""
    assert e.warmup_on_startup is True
    assert e.warmup_startup_timeout_s == 180


def test_stt_local_config_has_expected_defaults(tmp_path):
    s = SystemConfigStore(str(tmp_path / "system_config.json"))
    c = s.get().stt_local
    assert c.stt_model_dir == "models/stt"
    assert c.vosk_model_path == "models/stt/vosk-model-small-en-us-0.15"
    assert c.vosk_model_base_url == "https://alphacephei.com/vosk/models"
    assert c.stt_stream_sample_rate == 16000
    assert c.whisper_local_model == "phowhisper-medium"
    assert c.whisper_local_device == "cpu"
    assert c.whisper_local_compute_type == "int8"
    assert c.whisper_vad_filter is True
    assert c.whisper_beam_size == 1
    assert c.whisper_condition_on_previous_text is False
    assert c.whisper_initial_prompt == ""
    assert c.stt_glossary_path == ""
    assert c.stt_profile == ""
    assert c.whisper_mlx_model_path == "models/stt/phowhisper-medium-mlx"
    assert c.qwen3_asr_model == "Qwen/Qwen3-ASR-0.6B"
    assert c.qwen3_asr_device == ""
    assert c.stt_enhance_timeout_seconds == 30.0
    assert "ASR post-editor" in c.stt_enhance_prompt
    assert c.stt_segment_long_enabled is False
    assert c.stt_segment_min_seconds == 30.0
    assert c.stt_segment_concurrency == 4


def test_omnivoice_config_has_expected_defaults(tmp_path):
    s = SystemConfigStore(str(tmp_path / "system_config.json"))
    o = s.get().omnivoice
    assert o.omnivoice_path == "/Users/lugon/code/OmniVoice"
    assert o.omnivoice_model_id == "k2-fsa/OmniVoice"
    assert o.omnivoice_device == ""
    assert o.omnivoice_dtype == "float16"
    assert o.omnivoice_python == ""
    assert o.omnivoice_timeout_seconds == 45.0
    assert o.omnivoice_use_server is True
    assert o.omnivoice_server_host == "127.0.0.1"
    assert o.omnivoice_server_port == 8762
    assert o.omnivoice_server_startup_seconds == 60.0
    assert o.omnivoice_default_instruct == "female, young adult"
    assert o.omnivoice_class_temperature == 0.0
    assert o.omnivoice_pin_voice is True
    assert "giọng đọc tham chiếu" in o.omnivoice_ref_text
    assert o.default_tts_engine_voice == ""


def test_conversation_llm_config_has_expected_defaults(tmp_path):
    s = SystemConfigStore(str(tmp_path / "system_config.json"))
    c = s.get().conversation_llm
    assert c.conversation_llm_base_url == ""
    assert c.conversation_llm_api_key == ""
    assert c.conversation_llm_model == "gpt-3.5-turbo"
    assert c.conversation_llm_timeout_seconds == 60.0
    assert c.ollama_bin == ""


def test_remote_stt_config_has_expected_defaults(tmp_path):
    s = SystemConfigStore(str(tmp_path / "system_config.json"))
    c = s.get().remote_stt
    assert c.whisper_service_base_url == ""
    assert c.whisper_service_api_key == ""
    assert c.whisper_service_model == "whisper-1"
    assert c.eventlab_base_url == ""
    assert c.eventlab_api_key == ""
    assert c.eventlab_model == "whisper-1"
    assert c.remote_stt_timeout_seconds == 60.0


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
    assert c.conversation_stt_engine == "whisper"
    assert c.conversation_fast_stt_engine == ""
    assert c.conversation_fast_stt_max_ms == 1500
    assert c.conversation_streaming_stt is False
    assert c.conversation_streaming_chunk_ms == 1000
    assert c.conversation_tts_engine == "omnivoice"
    assert c.conversation_tts_lookahead == 3
    assert c.conversation_opus_pace is False
    assert c.conversation_opus_prebuffer_frames == 5
    assert c.conversation_language == "vi"
    assert "helpful, concise voice assistant" in c.conversation_system_prompt


def test_preprocessing_config_has_expected_defaults(tmp_path):
    s = SystemConfigStore(str(tmp_path / "system_config.json"))
    c = s.get().preprocessing
    assert c.stt_vad_enabled is False
    assert c.stt_vad_backend == "energy"
    assert c.stt_noise_reduce_enabled is False
    assert c.stt_noise_reduce_amount == 0.85
    assert c.pyannote_vad_model == "pyannote/segmentation-3.0"
    assert c.pyannote_auth_token == ""


def test_set_replaces_full_config_and_persists(tmp_path):
    from app.services.system_config import SystemConfig

    p = str(tmp_path / "system_config.json")
    s1 = SystemConfigStore(p)
    current = s1.get()
    updated = current.model_copy(
        update={"engines": current.engines.model_copy(update={"default_stt_engine": "qwen3_asr"})}
    )
    result = s1.set(updated)
    assert result.engines.default_stt_engine == "qwen3_asr"

    s2 = SystemConfigStore(p)
    assert s2.get().engines.default_stt_engine == "qwen3_asr"
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/unit/test_system_config_store.py -v`
Expected: FAIL — `AttributeError: 'SystemConfig' object has no attribute 'engines'` (and similarly for the other new groups/method).

- [ ] **Step 3: Implement the 7 nested models, extend `SystemConfig`, add `set()`**

Replace the `SystemConfig` class and add `set()` to `SystemConfigStore` in `apps/api_gateway/app/services/system_config.py`:

```python
class EngineDefaults(BaseModel):
    default_stt_engine: str = "vosk"
    default_tts_engine: str = "omnivoice"
    extra_warmup_stt_engines: str = ""
    extra_warmup_tts_engines: str = ""
    warmup_on_startup: bool = True
    warmup_startup_timeout_s: int = 180


class SttLocalConfig(BaseModel):
    stt_model_dir: str = "models/stt"
    vosk_model_path: str = "models/stt/vosk-model-small-en-us-0.15"
    vosk_model_base_url: str = "https://alphacephei.com/vosk/models"
    stt_stream_sample_rate: int = 16000
    whisper_local_model: str = "phowhisper-medium"
    whisper_local_device: str = "cpu"
    whisper_local_compute_type: str = "int8"
    whisper_vad_filter: bool = True
    whisper_beam_size: int = 1
    whisper_condition_on_previous_text: bool = False
    whisper_initial_prompt: str = ""
    stt_glossary_path: str = ""
    stt_profile: str = ""
    whisper_mlx_model_path: str = "models/stt/phowhisper-medium-mlx"
    qwen3_asr_model: str = "Qwen/Qwen3-ASR-0.6B"
    qwen3_asr_device: str = ""
    stt_enhance_timeout_seconds: float = 30.0
    stt_enhance_prompt: str = (
        "You are an ASR post-editor. Fix spelling, casing, punctuation and obvious "
        "speech-recognition errors in the transcript. Do NOT translate, do NOT answer it, "
        "do NOT add or remove meaning. Return ONLY the corrected transcript text."
    )
    stt_segment_long_enabled: bool = False
    stt_segment_min_seconds: float = 30.0
    stt_segment_concurrency: int = 4


class OmnivoiceConfig(BaseModel):
    omnivoice_path: str = "/Users/lugon/code/OmniVoice"
    omnivoice_model_id: str = "k2-fsa/OmniVoice"
    omnivoice_device: str = ""
    omnivoice_dtype: str = "float16"
    omnivoice_python: str = ""
    omnivoice_timeout_seconds: float = 45.0
    omnivoice_use_server: bool = True
    omnivoice_server_host: str = "127.0.0.1"
    omnivoice_server_port: int = 8762
    omnivoice_server_startup_seconds: float = 60.0
    omnivoice_default_instruct: str = "female, young adult"
    omnivoice_class_temperature: float = 0.0
    omnivoice_pin_voice: bool = True
    omnivoice_ref_text: str = "Xin chào, đây là giọng đọc tham chiếu để giữ giọng nhất quán."
    default_tts_engine_voice: str = ""

    @property
    def omnivoice_python_path(self) -> str:
        return self.omnivoice_python or f"{self.omnivoice_path.rstrip('/')}/.venv/bin/python"


class ConversationLlmConfig(BaseModel):
    conversation_llm_base_url: str = ""
    conversation_llm_api_key: str = ""
    conversation_llm_model: str = "gpt-3.5-turbo"
    conversation_llm_timeout_seconds: float = 60.0
    ollama_bin: str = ""


class RemoteSttConfig(BaseModel):
    whisper_service_base_url: str = ""
    whisper_service_api_key: str = ""
    whisper_service_model: str = "whisper-1"
    eventlab_base_url: str = ""
    eventlab_api_key: str = ""
    eventlab_model: str = "whisper-1"
    remote_stt_timeout_seconds: float = 60.0


class ConversationTuningConfig(BaseModel):
    conversation_silence_ms: int = 700
    conversation_min_silence_ms: int = 450
    conversation_adaptive_full_ms: int = 3000
    conversation_min_speech_ms: int = 300
    conversation_rms_threshold: float = 0.015
    conversation_preroll_ms: int = 600
    conversation_max_utterance_ms: int = 30000
    conversation_goodbye_text: str = "Hẹn gặp lại nha!"
    conversation_stt_engine: str = "whisper"
    conversation_fast_stt_engine: str = ""
    conversation_fast_stt_max_ms: int = 1500
    conversation_streaming_stt: bool = False
    conversation_streaming_chunk_ms: int = 1000
    conversation_tts_engine: str = "omnivoice"
    conversation_tts_lookahead: int = 3
    conversation_opus_pace: bool = False
    conversation_opus_prebuffer_frames: int = 5
    conversation_language: str = "vi"
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


class PreprocessingConfig(BaseModel):
    stt_vad_enabled: bool = False
    stt_vad_backend: str = "energy"
    stt_noise_reduce_enabled: bool = False
    stt_noise_reduce_amount: float = 0.85
    pyannote_vad_model: str = "pyannote/segmentation-3.0"
    pyannote_auth_token: str = ""


class SystemConfig(BaseModel):
    base_context: str = ""
    openrouter_api_key: str = ""
    engines: EngineDefaults = EngineDefaults()
    stt_local: SttLocalConfig = SttLocalConfig()
    omnivoice: OmnivoiceConfig = OmnivoiceConfig()
    conversation_llm: ConversationLlmConfig = ConversationLlmConfig()
    remote_stt: RemoteSttConfig = RemoteSttConfig()
    conversation: ConversationTuningConfig = ConversationTuningConfig()
    preprocessing: PreprocessingConfig = PreprocessingConfig()
```

Add to `SystemConfigStore` (after `set_openrouter_api_key`):

```python
    def set(self, config: SystemConfig) -> SystemConfig:
        with self._lock:
            self._ensure()
            self._put(config)
            self._cache = config
            return config
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `pytest tests/unit/test_system_config_store.py -v`
Expected: PASS (all tests, including the pre-existing ones — `_ensure`'s legacy-import path still only touches `base_context`/`openrouter_api_key`, since the legacy JSON files never had the new groups; `SystemConfig()` defaults fill them in, which is exactly what the new default tests assert).

- [ ] **Step 5: Write failing tests for route-level masking of all 5 secrets and full nested round-trip**

Append to `tests/unit/test_system_config_routes.py`:

```python
def test_get_config_includes_nested_groups_with_defaults(client):
    data = client.get("/v1/system/config").json()["data"]
    assert data["engines"]["default_stt_engine"] == "vosk"
    assert data["stt_local"]["whisper_local_model"] == "phowhisper-medium"
    assert data["omnivoice"]["omnivoice_model_id"] == "k2-fsa/OmniVoice"
    assert data["conversation_llm"]["conversation_llm_model"] == "gpt-3.5-turbo"
    assert data["remote_stt"]["whisper_service_model"] == "whisper-1"
    assert data["conversation"]["conversation_silence_ms"] == 700
    assert data["preprocessing"]["stt_vad_backend"] == "energy"


def test_put_updates_a_nested_field_and_preserves_others(client):
    full = client.get("/v1/system/config").json()["data"]
    full["engines"]["default_stt_engine"] = "qwen3_asr"
    resp = client.put("/v1/system/config", json=full)
    data = resp.json()["data"]
    assert data["engines"]["default_stt_engine"] == "qwen3_asr"
    assert data["stt_local"]["whisper_local_model"] == "phowhisper-medium"  # unrelated group untouched


@pytest.mark.parametrize(
    "group,field",
    [
        (None, "openrouter_api_key"),
        ("conversation_llm", "conversation_llm_api_key"),
        ("remote_stt", "whisper_service_api_key"),
        ("remote_stt", "eventlab_api_key"),
        ("preprocessing", "pyannote_auth_token"),
    ],
)
def test_secret_field_is_masked_and_blank_put_preserves_it(client, group, field):
    full = client.get("/v1/system/config").json()["data"]
    target = full if group is None else full[group]
    target[field] = "super-secret-value"
    masked = client.put("/v1/system/config", json=full).json()["data"]
    masked_target = masked if group is None else masked[group]
    assert masked_target[field] == "***"

    # Re-submit the whole form with the mask placeholder still in place (as the UI would).
    resubmit = client.get("/v1/system/config").json()["data"]
    still_masked = client.put("/v1/system/config", json=resubmit).json()["data"]
    still_masked_target = still_masked if group is None else still_masked[group]
    assert still_masked_target[field] == "***"  # still configured, not wiped
```

- [ ] **Step 6: Run tests, verify they fail**

Run: `pytest tests/unit/test_system_config_routes.py -v`
Expected: FAIL — `KeyError: 'engines'` (response body doesn't have the nested groups yet), then later masking assertions failing once that's fixed.

- [ ] **Step 7: Rewrite the route to use `SystemConfig` directly, with full masking/merge**

Replace in `apps/api_gateway/app/api/routes/system.py`:

```python
from app.services.system_config import SystemConfig, system_config_store
```

Remove the `SystemConfigRequest` class entirely (it duplicated `SystemConfig` 1:1 — using `SystemConfig` itself as the request body removes the drift risk). Replace `_mask_system_config` and the two route handlers:

```python
def _mask_system_config(config: SystemConfig) -> dict:
    data = config.model_dump()
    if data.get("openrouter_api_key"):
        data["openrouter_api_key"] = "***"
    if data["conversation_llm"].get("conversation_llm_api_key"):
        data["conversation_llm"]["conversation_llm_api_key"] = "***"
    if data["remote_stt"].get("whisper_service_api_key"):
        data["remote_stt"]["whisper_service_api_key"] = "***"
    if data["remote_stt"].get("eventlab_api_key"):
        data["remote_stt"]["eventlab_api_key"] = "***"
    if data["preprocessing"].get("pyannote_auth_token"):
        data["preprocessing"]["pyannote_auth_token"] = "***"
    return data


def _merge_system_config(current: SystemConfig, payload: SystemConfig) -> SystemConfig:
    """Blank or '***' in an incoming secret field means "keep the existing value" —
    the UI never re-sends a real secret it fetched, only a fresh one the user typed."""
    update = payload.model_dump()

    def _keep_if_blank_or_masked(new_value: str, old_value: str) -> str:
        return old_value if (not new_value or new_value == "***") else new_value

    update["openrouter_api_key"] = _keep_if_blank_or_masked(
        update["openrouter_api_key"], current.openrouter_api_key
    )
    update["conversation_llm"]["conversation_llm_api_key"] = _keep_if_blank_or_masked(
        update["conversation_llm"]["conversation_llm_api_key"],
        current.conversation_llm.conversation_llm_api_key,
    )
    update["remote_stt"]["whisper_service_api_key"] = _keep_if_blank_or_masked(
        update["remote_stt"]["whisper_service_api_key"],
        current.remote_stt.whisper_service_api_key,
    )
    update["remote_stt"]["eventlab_api_key"] = _keep_if_blank_or_masked(
        update["remote_stt"]["eventlab_api_key"], current.remote_stt.eventlab_api_key
    )
    update["preprocessing"]["pyannote_auth_token"] = _keep_if_blank_or_masked(
        update["preprocessing"]["pyannote_auth_token"],
        current.preprocessing.pyannote_auth_token,
    )
    return SystemConfig.model_validate(update)


@router.get("/system/config")
async def get_system_config() -> dict:
    return {"success": True, "data": _mask_system_config(system_config_store.get())}


@router.put("/system/config")
async def set_system_config(payload: SystemConfig) -> dict:
    current = system_config_store.get()
    merged = _merge_system_config(current, payload)
    new_config = system_config_store.set(merged)
    return {"success": True, "data": _mask_system_config(new_config)}
```

Note: no cache-invalidation call here yet — Tasks 6 and 7 each add one `if` branch that calls their hook, right after `system_config_store.set(merged)`. Leave a marker comment so those tasks know where to add it:

```python
    new_config = system_config_store.set(merged)
    # Cache-invalidation hooks for settings cached at boot/first-use are added here
    # incrementally (remote_stt in Task 6, omnivoice in Task 7, preprocessing.pyannote_*
    # and stt_local.qwen3_asr_device in Tasks 5/4 respectively).
    return {"success": True, "data": _mask_system_config(new_config)}
```

- [ ] **Step 8: Run tests, verify they pass**

Run: `pytest tests/unit/test_system_config_routes.py tests/unit/test_system_config_store.py tests/unit/test_system_config.py -v`
Expected: PASS.

- [ ] **Step 9: Rename the JS module and update its 5 importers**

`git mv apps/api_gateway/app/static/js/base-context.js apps/api_gateway/app/static/js/system-config.js`

In each of `apps/api_gateway/app/static/js/conversation.js`, `stt-batch.js`, `stt-stream.js`, `chat.js`, `livehost.js`, change:
```js
import { getPreproc } from "./base-context.js";
```
to:
```js
import { getPreproc } from "./system-config.js";
```

- [ ] **Step 10: Extend `system-config.js` with the 7 grouped sections**

Add to `apps/api_gateway/app/static/js/system-config.js` (after the existing `loadOpenrouterKeyStatus`/`saveOpenrouterKey` block, before `getPreproc`):

```javascript
const GROUPS = [
  { key: "engines", label: "Engine Defaults", open: true },
  { key: "stt_local", label: "STT (Local Models)", open: false },
  { key: "omnivoice", label: "OmniVoice (TTS)", open: false },
  { key: "conversation_llm", label: "Conversation LLM", open: false },
  { key: "remote_stt", label: "Remote STT Providers", open: false },
  { key: "conversation", label: "Conversation Tuning", open: false },
  { key: "preprocessing", label: "Preprocessing (VAD/Noise)", open: false },
];

const SECRET_FIELDS = new Set([
  "conversation_llm.conversation_llm_api_key",
  "remote_stt.whisper_service_api_key",
  "remote_stt.eventlab_api_key",
  "preprocessing.pyannote_auth_token",
]);

function fieldInputType(value) {
  if (typeof value === "boolean") return "checkbox";
  if (typeof value === "number") return "number";
  return "text";
}

function renderGroupFields(groupKey, groupValue) {
  return Object.entries(groupValue)
    .map(([field, value]) => {
      const id = `sys-${groupKey}-${field}`;
      const isSecret = SECRET_FIELDS.has(`${groupKey}.${field}`);
      const type = isSecret ? "password" : fieldInputType(value);
      const checked = type === "checkbox" && value ? "checked" : "";
      const val = type === "checkbox" ? "" : `value="${isSecret ? "" : String(value)}"`;
      const placeholder = isSecret && value ? `placeholder="${value ? "***" : ""}"` : "";
      return `<label class="field">${field}
        <input type="${type}" id="${id}" ${val} ${checked} ${placeholder} />
      </label>`;
    })
    .join("\n");
}

export async function loadSystemConfigGroups() {
  const body = await (await fetch("/v1/system/config")).json();
  const root = el("sys-config-groups");
  if (!root) return;
  root.innerHTML = GROUPS.map(
    (g) => `<details ${g.open ? "open" : ""}>
      <summary>${g.label}</summary>
      <div class="fields">${renderGroupFields(g.key, body.data[g.key])}</div>
    </details>`
  ).join("\n");
}

export async function saveSystemConfigGroups() {
  const status = el("sys-config-groups-status");
  try {
    const current = await (await fetch("/v1/system/config")).json();
    const payload = current.data;
    for (const g of GROUPS) {
      for (const field of Object.keys(payload[g.key])) {
        const input = el(`sys-${g.key}-${field}`);
        if (!input) continue;
        payload[g.key][field] =
          input.type === "checkbox"
            ? input.checked
            : input.type === "number"
              ? Number(input.value)
              : input.value;
      }
    }
    const resp = await fetch("/v1/system/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await resp.json();
    if (!resp.ok) { print(status, body.detail || JSON.stringify(body), true); return; }
    status.classList.remove("error");
    status.textContent = "Saved ✓ (remote STT / OmniVoice / VAD changes apply automatically, no restart needed)";
    await loadSystemConfigGroups();
  } catch (error) {
    print(status, String(error), true);
  }
}
if (el("sys-config-groups-save")) {
  el("sys-config-groups-save").addEventListener("click", saveSystemConfigGroups);
  loadSystemConfigGroups();
}
```

- [ ] **Step 11: Add the panel markup to `index.html`**

In `apps/api_gateway/app/static/index.html`, inside `#section-system`, right after the existing "OpenRouter API key" `<section class="card">` block (before "STT preprocessing"), add:

```html
            <section class="card">
              <h2>System settings</h2>
              <p class="hint">Engine choices, model paths, LLM/remote-STT endpoints, and conversation tuning. Changes take effect immediately — no restart needed.</p>
              <div id="sys-config-groups"></div>
              <div class="actions end">
                <button id="sys-config-groups-save">Save</button>
              </div>
              <p id="sys-config-groups-status" class="meta"></p>
            </section>
```

Also update the now-stale hint text right above it (originally: `"Engines, devices and preprocessing come from environment config (see .env)."`) to:

```html
              <p class="hint">Manage downloadable models in the <strong>Models</strong> section. Engine/model/LLM/tuning config has moved to System settings below.</p>
```

- [ ] **Step 12: Manual browser check**

Start the app (`make dev` or equivalent), open the control panel, go to the System tab, confirm the 7 collapsible groups render with correct defaults, edit one field (e.g. `engines.default_stt_engine`), save, reload the page, confirm the edited value persisted and unrelated fields are untouched.

- [ ] **Step 13: Run the full test suite**

Run: `pytest`
Expected: PASS, no regressions (app behavior is unchanged — no consumer reads the new fields yet).

- [ ] **Step 14: Commit**

```bash
git add apps/api_gateway/app/services/system_config.py apps/api_gateway/app/api/routes/system.py \
  apps/api_gateway/app/static/js/system-config.js apps/api_gateway/app/static/js/conversation.js \
  apps/api_gateway/app/static/js/stt-batch.js apps/api_gateway/app/static/js/stt-stream.js \
  apps/api_gateway/app/static/js/chat.js apps/api_gateway/app/static/js/livehost.js \
  apps/api_gateway/app/static/index.html \
  tests/unit/test_system_config_store.py tests/unit/test_system_config_routes.py
git add apps/api_gateway/app/static/js/base-context.js  # git mv leaves the old path staged as a delete
git commit -m "feat(system-config): add nested settings groups, generalize secret masking, extend admin UI"
```

---

### Task 2: Engine Defaults migration

**Files:**
- Modify: `apps/api_gateway/app/api/routes/stt.py:47,160`
- Modify: `apps/api_gateway/app/services/stt/profile.py:54-67` (only the `default_stt_engine` half of the or-chain — `conversation_stt_engine` stays as `settings.conversation_stt_engine` until Task 3)
- Modify: `apps/api_gateway/app/api/routes/conversation.py:210`, `apps/api_gateway/app/api/routes/livehost.py:115`, `apps/api_gateway/app/api/routes/lugo.py:58` (only the `default_tts_engine` half of each or-chain)
- Modify: `apps/api_gateway/app/services/tts/service.py:35`
- Modify: `apps/api_gateway/app/core/settings.py` (delete `warmup_stt_engines`/`warmup_tts_engines` properties, replace with functions in `system_config.py`)
- Modify: `apps/api_gateway/app/services/warmup.py:48-51`
- Modify: `apps/api_gateway/app/main.py:143-150`
- Test: `tests/unit/test_stt_routes.py` or wherever the existing `/v1/stt/transcribe` route tests live (grep to confirm), `tests/unit/test_warmup.py` (grep to confirm actual path)

**Interfaces:**
- Consumes: `system_config_store.get().engines` (from Task 1) — `default_stt_engine`, `default_tts_engine`, `extra_warmup_stt_engines`, `extra_warmup_tts_engines`, `warmup_on_startup`, `warmup_startup_timeout_s`.
- Produces: `apps/api_gateway/app/services/system_config.py` gains two module-level functions `warmup_stt_engines() -> list[str]` and `warmup_tts_engines() -> list[str]` (moved from `Settings` properties, same logic, reading `system_config_store.get().engines` + `system_config_store.get().conversation.conversation_stt_engine/conversation_tts_engine` — NOTE these two properties combine an Engine-Defaults field with a Conversation-Tuning field; since Task 3 hasn't migrated `conversation_stt_engine`/`conversation_tts_engine` yet, this function reads those two fields from `settings` still, and Task 3 updates just those two reads in the same function).

- [ ] **Step 1: `stt.py:47` — FastAPI `Form` default can't call `system_config_store` at import time**

Current:
```python
@router.post("/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    engine: str = Form(default=settings.default_stt_engine),
    language: str | None = Form(default=None),
    denoise: bool | None = Form(default=None),
    vad: bool | None = Form(default=None),
    vad_backend: str | None = Form(default=None),
    segment: bool | None = Form(default=None),
) -> dict:
    payload = STTRequest(engine=engine, language=language)
```

New — move the default resolution inside the function body (`Form(default=None)`, then resolve after entry):
```python
@router.post("/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    engine: str | None = Form(default=None),
    language: str | None = Form(default=None),
    denoise: bool | None = Form(default=None),
    vad: bool | None = Form(default=None),
    vad_backend: str | None = Form(default=None),
    segment: bool | None = Form(default=None),
) -> dict:
    engine = engine or system_config_store.get().engines.default_stt_engine
    payload = STTRequest(engine=engine, language=language)
```
Add `from app.services.system_config import system_config_store` to the file's imports if not already present (it is not, per Task 1's research — `system.py` has it, `stt.py` currently does not).

- [ ] **Step 2: `stt.py:160` — websocket query-param fallback**

Current:
```python
    engine = websocket.query_params.get("engine", settings.default_stt_engine)
```
New:
```python
    engine = websocket.query_params.get("engine", system_config_store.get().engines.default_stt_engine)
```

- [ ] **Step 3: `profile.py` — `resolve_stt`'s `default_stt_engine` half only**

Current (full function, showing all fields including ones NOT touched this task):
```python
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
```
New (only the last line of the `engine` chain changes; `settings.stt_profile` and `settings.conversation_stt_engine` are untouched — Task 4 and Task 3 respectively):
```python
    from app.core.settings import settings
    from app.services.system_config import system_config_store

    stt_cfg = getattr(profile, "stt", None)
    preset_name = (getattr(stt_cfg, "profile", "") or "") or settings.stt_profile
    preset = resolve_stt_profile(preset_name)
    preset_engine, preset_lang = preset if preset else (None, None)

    engine = (
        q_engine
        or (getattr(stt_cfg, "engine", "") or None)
        or preset_engine
        or settings.conversation_stt_engine
        or system_config_store.get().engines.default_stt_engine
    )
```

- [ ] **Step 4: `conversation.py:210`, `livehost.py:115`, `lugo.py:58` — `default_tts_engine` half of each or-chain**

`conversation.py`, current:
```python
        tts_engine = q.get("tts_engine") or settings.conversation_tts_engine or settings.default_tts_engine
```
New:
```python
        tts_engine = (
            q.get("tts_engine")
            or settings.conversation_tts_engine
            or system_config_store.get().engines.default_tts_engine
        )
```
Apply the identical change to `livehost.py:115` (same literal line) and to `lugo.py:58`'s dict-literal form:
```python
        tts = dict(engine=settings.conversation_tts_engine or settings.default_tts_engine,
```
→
```python
        tts = dict(
            engine=settings.conversation_tts_engine or system_config_store.get().engines.default_tts_engine,
```
Add `from app.services.system_config import system_config_store` to each of the 3 files if not already imported (confirm via grep before adding — avoid duplicate imports).

- [ ] **Step 5: `tts/service.py:35` — `list_engines`'s `"default"` flag**

Current:
```python
    def list_engines(self) -> list[dict]:
        result: list[dict] = []
        for name, provider in self.providers.items():
            result.append(
                {
                    "engine": name,
                    "available": provider.available(),
                    "detail": provider.detail(),
                    "install_hint": provider.install_hint(),
                    "default": name == settings.default_tts_engine,
                }
            )
        return result
```
New:
```python
    def list_engines(self) -> list[dict]:
        result: list[dict] = []
        default_engine = system_config_store.get().engines.default_tts_engine
        for name, provider in self.providers.items():
            result.append(
                {
                    "engine": name,
                    "available": provider.available(),
                    "detail": provider.detail(),
                    "install_hint": provider.install_hint(),
                    "default": name == default_engine,
                }
            )
        return result
```
Add `from app.services.system_config import system_config_store` to the file's imports.

- [ ] **Step 6: Move `warmup_stt_engines`/`warmup_tts_engines` out of `Settings`**

Delete from `apps/api_gateway/app/core/settings.py`:
```python
    @property
    def warmup_stt_engines(self) -> list[str]:
        extra = [e.strip() for e in self.extra_warmup_stt_engines.split(",") if e.strip()]
        seen: list[str] = []
        for engine in [self.conversation_stt_engine, *extra]:
            if engine and engine not in seen:
                seen.append(engine)
        return seen

    @property
    def warmup_tts_engines(self) -> list[str]:
        extra = [e.strip() for e in self.extra_warmup_tts_engines.split(",") if e.strip()]
        seen: list[str] = []
        for engine in [self.conversation_tts_engine, *extra]:
            if engine and engine not in seen:
                seen.append(engine)
        return seen
```
Also delete the now-migrated fields from `Settings`: `default_stt_engine`, `default_tts_engine`, `extra_warmup_stt_engines`, `extra_warmup_tts_engines`, `warmup_on_startup`, `warmup_startup_timeout_s`.

Add to `apps/api_gateway/app/services/system_config.py` (module level, after the `SystemConfigStore` class):
```python
def warmup_stt_engines() -> list[str]:
    engines = system_config_store.get().engines
    extra = [e.strip() for e in engines.extra_warmup_stt_engines.split(",") if e.strip()]
    seen: list[str] = []
    for engine in [settings.conversation_stt_engine, *extra]:  # migrated to system_config_store in Task 3
        if engine and engine not in seen:
            seen.append(engine)
    return seen


def warmup_tts_engines() -> list[str]:
    engines = system_config_store.get().engines
    extra = [e.strip() for e in engines.extra_warmup_tts_engines.split(",") if e.strip()]
    seen: list[str] = []
    for engine in [settings.conversation_tts_engine, *extra]:  # migrated to system_config_store in Task 3
        if engine and engine not in seen:
            seen.append(engine)
    return seen
```
(`settings` is already imported at the top of `system_config.py`.)

- [ ] **Step 7: Write a failing test for the moved warm-up functions**

Add to `tests/unit/test_system_config_store.py`:
```python
def test_warmup_stt_engines_combines_conversation_default_and_extras(tmp_path, monkeypatch):
    from app.core.settings import settings
    from app.services import system_config as sc_mod

    fresh = SystemConfigStore(str(tmp_path / "system_config.json"))
    fresh.set(
        fresh.get().model_copy(
            update={"engines": fresh.get().engines.model_copy(update={"extra_warmup_stt_engines": "qwen3_asr, whisper_mlx"})}
        )
    )
    monkeypatch.setattr(sc_mod, "system_config_store", fresh)
    result = sc_mod.warmup_stt_engines()
    assert result == [settings.conversation_stt_engine, "qwen3_asr", "whisper_mlx"]
```

- [ ] **Step 8: Run test, verify it fails, then update `warmup.py` and `main.py` call sites**

Run: `pytest tests/unit/test_system_config_store.py::test_warmup_stt_engines_combines_conversation_default_and_extras -v`
Expected: FAIL — `AttributeError: module 'app.services.system_config' has no attribute 'warmup_stt_engines'` (until Step 6 lands, which it did above — actually run this AFTER step 6; expected PASS once Step 6's code exists). Re-run to confirm PASS before continuing.

Update `apps/api_gateway/app/services/warmup.py`:
```python
    for e in settings.warmup_stt_engines:
        _add(stt, e)
    for e in settings.warmup_tts_engines:
        _add(tts, e)
```
→
```python
    from app.services.system_config import warmup_stt_engines, warmup_tts_engines

    for e in warmup_stt_engines():
        _add(stt, e)
    for e in warmup_tts_engines():
        _add(tts, e)
```

Update `apps/api_gateway/app/main.py`:
```python
    if settings.warmup_on_startup:
        try:
            await asyncio.wait_for(_warm_default_engines(), timeout=settings.warmup_startup_timeout_s)
        except TimeoutError:
            logger.warning(
                "boot warm-up exceeded %ss — serving anyway; the first turn may be cold",
                settings.warmup_startup_timeout_s,
            )
    yield
```
→
```python
    engine_defaults = system_config_store.get().engines
    if engine_defaults.warmup_on_startup:
        try:
            await asyncio.wait_for(_warm_default_engines(), timeout=engine_defaults.warmup_startup_timeout_s)
        except TimeoutError:
            logger.warning(
                "boot warm-up exceeded %ss — serving anyway; the first turn may be cold",
                engine_defaults.warmup_startup_timeout_s,
            )
    yield
```
Add `from app.services.system_config import system_config_store` to `main.py`'s imports if not already present (it likely isn't at module scope — confirm via grep).

- [ ] **Step 9: Update existing tests that construct requests/routes relying on the old defaults**

Grep `tests/` for `default_stt_engine`, `default_tts_engine`, `warmup_on_startup`, `warmup_startup_timeout_s`, `extra_warmup_`, `warmup_stt_engines`, `warmup_tts_engines` and update each hit: replace `settings.<field>` / `monkeypatch.setattr(settings, "<field>", ...)` with the `system_config_store` equivalent, following the fixture pattern from `tests/unit/test_stt_service_openrouter.py` (`SystemConfigStore(str(tmp_path/...))` + `monkeypatch.setattr("<module>.system_config_store", fresh)`).

- [ ] **Step 10: Run the full test suite**

Run: `pytest`
Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add apps/api_gateway/app/api/routes/stt.py apps/api_gateway/app/services/stt/profile.py \
  apps/api_gateway/app/api/routes/conversation.py apps/api_gateway/app/api/routes/livehost.py \
  apps/api_gateway/app/api/routes/lugo.py apps/api_gateway/app/services/tts/service.py \
  apps/api_gateway/app/core/settings.py apps/api_gateway/app/services/system_config.py \
  apps/api_gateway/app/services/warmup.py apps/api_gateway/app/main.py tests/
git commit -m "feat(system-config): migrate engine-default/warmup settings off .env"
```

---

### Task 3: Conversation LLM + Conversation Tuning migration

**Files:**
- Modify: `apps/api_gateway/app/services/llm_models.py` (all `conversation_llm_*`, `ollama_bin` sites — 10+8 sites)
- Modify: `apps/api_gateway/app/services/conversation/responder.py` (`conversation_llm_*`, `conversation_llm_timeout_seconds`, `conversation_system_prompt`)
- Modify: `apps/api_gateway/app/services/recommend/service.py:86-89`, `apps/api_gateway/app/services/recommend/capabilities.py` (`ollama_bin` site)
- Modify: `apps/api_gateway/app/services/stt/providers/whisper_gemma_provider.py:32-38` (`conversation_llm_base_url`/`api_key` only — `stt_enhance_*` stays for Task 4)
- Modify: `apps/api_gateway/app/services/memory/compactor.py`, `embedder.py`, `extractor.py` (`conversation_llm_timeout_seconds`)
- Modify: `apps/api_gateway/app/services/conversation/session.py` (VadEndpointer fields, fast-STT, tts_lookahead, opus pacing) — leave `settings.stt_noise_reduce_amount` at line ~471 untouched (Task 5)
- Modify: `apps/api_gateway/app/api/routes/livehost.py` (duplicate VadEndpointer construction, tts_lookahead, opus pacing, `conversation_tts_engine` half of the or-chain from Task 2's Step 4)
- Modify: `apps/api_gateway/app/api/routes/lugo.py` (`conversation_goodbye_text`, `conversation_tts_engine` half of Task 2's or-chain)
- Modify: `apps/api_gateway/app/api/routes/conversation.py:127,210` (`conversation_system_prompt`, `conversation_tts_engine` half)
- Modify: `apps/api_gateway/app/services/stt/profile.py` (`conversation_stt_engine`, `conversation_language` — the other half of Step 3's or-chain from Task 2, plus a fully independent field)
- Modify: `apps/api_gateway/app/services/system_config.py` (fix the two `settings.conversation_stt_engine`/`conversation_tts_engine` reads left inside `warmup_stt_engines()`/`warmup_tts_engines()` from Task 2 Step 6)
- Modify: `apps/api_gateway/app/core/settings.py` (delete all migrated fields; note `conversation_streaming_stt`/`conversation_streaming_chunk_ms` have zero call sites — delete the fields, no consumer step needed for them)
- Test: existing test files covering `llm_models.py`, `responder.py`, `session.py` (grep to find exact paths)

**Interfaces:**
- Consumes: `system_config_store.get().conversation_llm` and `system_config_store.get().conversation` (from Task 1).

- [ ] **Step 1: `llm_models.py` — all `conversation_llm_base_url`/`api_key`/`ollama_bin` sites**

Add `from app.services.system_config import system_config_store` to imports. Replace each of the 6 `settings.conversation_llm_base_url` reads and 2 `settings.conversation_llm_api_key` reads:

```python
def _ollama_base() -> str:
    base = settings.conversation_llm_base_url.rstrip("/")
    return base[:-3].rstrip("/") if base.endswith("/v1") else base
```
→
```python
def _ollama_base() -> str:
    base = system_config_store.get().conversation_llm.conversation_llm_base_url.rstrip("/")
    return base[:-3].rstrip("/") if base.endswith("/v1") else base
```

```python
def _is_remote_endpoint() -> bool:
    from urllib.parse import urlparse
    host = urlparse(settings.conversation_llm_base_url).hostname or ""
    return bool(host) and host not in ("localhost", "127.0.0.1", "0.0.0.0", "::1") \
        and not host.startswith("192.168.") and not host.endswith(".local")
```
→
```python
def _is_remote_endpoint() -> bool:
    from urllib.parse import urlparse
    host = urlparse(system_config_store.get().conversation_llm.conversation_llm_base_url).hostname or ""
    return bool(host) and host not in ("localhost", "127.0.0.1", "0.0.0.0", "::1") \
        and not host.startswith("192.168.") and not host.endswith(".local")
```

```python
    def available(self) -> bool:
        if _is_remote_endpoint():
            return bool(settings.conversation_llm_base_url and settings.conversation_llm_api_key)
```
→
```python
    def available(self) -> bool:
        if _is_remote_endpoint():
            cl = system_config_store.get().conversation_llm
            return bool(cl.conversation_llm_base_url and cl.conversation_llm_api_key)
```

```python
        return {
            "available": available,
            "remote": remote,
            "base_url": settings.conversation_llm_base_url,
            "active": active,
```
→
```python
        return {
            "available": available,
            "remote": remote,
            "base_url": system_config_store.get().conversation_llm.conversation_llm_base_url,
            "active": active,
```

```python
    def select(self, model: str) -> None:
        self.validate(model)
        base = settings.conversation_llm_base_url or "http://localhost:11434/v1"
        set_active_llm_config(base, settings.conversation_llm_api_key, model)
```
→
```python
    def select(self, model: str) -> None:
        self.validate(model)
        cl = system_config_store.get().conversation_llm
        base = cl.conversation_llm_base_url or "http://localhost:11434/v1"
        set_active_llm_config(base, cl.conversation_llm_api_key, model)
```

```python
    async def start_service(self, warm: bool = True) -> dict:
        if not settings.conversation_llm_base_url:
            raise AppError("CONVERSATION_LLM_BASE_URL is not set")
```
→
```python
    async def start_service(self, warm: bool = True) -> dict:
        if not system_config_store.get().conversation_llm.conversation_llm_base_url:
            raise AppError("conversation_llm.conversation_llm_base_url is not set")
```

```python
        return {
            "available": True,
            "started": started,
            "warmed": warmed,
            "active": active,
            "base_url": settings.conversation_llm_base_url,
        }
```
→
```python
        return {
            "available": True,
            "started": started,
            "warmed": warmed,
            "active": active,
            "base_url": system_config_store.get().conversation_llm.conversation_llm_base_url,
        }
```

```python
def _ollama_bin() -> str | None:
    candidates = [settings.ollama_bin, shutil.which("ollama"), "/opt/homebrew/opt/ollama/bin/ollama"]
```
→
```python
def _ollama_bin() -> str | None:
    candidates = [
        system_config_store.get().conversation_llm.ollama_bin,
        shutil.which("ollama"),
        "/opt/homebrew/opt/ollama/bin/ollama",
    ]
```

- [ ] **Step 2: `recommend/capabilities.py` — `ollama_bin` site**

```python
def _ollama() -> bool:
    try:
        if settings.ollama_bin and os.path.exists(settings.ollama_bin):
            return True
```
→
```python
def _ollama() -> bool:
    try:
        ollama_bin = system_config_store.get().conversation_llm.ollama_bin
        if ollama_bin and os.path.exists(ollama_bin):
            return True
```
Add the `system_config_store` import.

- [ ] **Step 3: `recommend/service.py:86-89` — `conversation_llm_base_url` (leave `whisper_service_base_url`/`eventlab_base_url` for Task 6)**

Current:
```python
def _augment_config_flags(caps: Capabilities) -> None:
    caps.modules["whisper_service"] = bool(settings.whisper_service_base_url)
    caps.modules["eventlab"] = bool(settings.eventlab_base_url)
    caps.modules["online_llm"] = bool(settings.conversation_llm_base_url)
    caps.modules["openrouter"] = bool(system_config_store.get().openrouter_api_key)
```
New:
```python
def _augment_config_flags(caps: Capabilities) -> None:
    caps.modules["whisper_service"] = bool(settings.whisper_service_base_url)
    caps.modules["eventlab"] = bool(settings.eventlab_base_url)
    caps.modules["online_llm"] = bool(system_config_store.get().conversation_llm.conversation_llm_base_url)
    caps.modules["openrouter"] = bool(system_config_store.get().openrouter_api_key)
```

- [ ] **Step 4: `responder.py` — `get_active_llm_base_url`/`get_active_llm_api_key`/`get_active_llm_model`, `conversation_llm_timeout_seconds`, `conversation_system_prompt`**

```python
def get_active_llm_base_url() -> str:
    return settings.conversation_llm_base_url if _active_base_url is None else _active_base_url
```
→
```python
def get_active_llm_base_url() -> str:
    if _active_base_url is not None:
        return _active_base_url
    return system_config_store.get().conversation_llm.conversation_llm_base_url
```

```python
def get_active_llm_api_key() -> str:
    return settings.conversation_llm_api_key if _active_api_key is None else _active_api_key
```
→
```python
def get_active_llm_api_key() -> str:
    if _active_api_key is not None:
        return _active_api_key
    return system_config_store.get().conversation_llm.conversation_llm_api_key
```

```python
def get_active_llm_model() -> str:
    return _active_model or settings.conversation_llm_model
```
→
```python
def get_active_llm_model() -> str:
    return _active_model or system_config_store.get().conversation_llm.conversation_llm_model
```

Both `build_responder`/`build_responder_ex`'s `timeout=settings.conversation_llm_timeout_seconds` become `timeout=system_config_store.get().conversation_llm.conversation_llm_timeout_seconds`.

```python
    persona = system_prompt if system_prompt is not None else settings.conversation_system_prompt
    base_context = system_config_store.get().base_context
```
→
```python
    persona = (
        system_prompt
        if system_prompt is not None
        else system_config_store.get().conversation.conversation_system_prompt
    )
    base_context = system_config_store.get().base_context
```
(`system_config_store` is already imported in this file, per the research — used for `base_context` already.)

- [ ] **Step 5: `whisper_gemma_provider.py:32-38` — only `conversation_llm_base_url`/`conversation_llm_api_key`**

Current:
```python
    async def _refine(self, text: str, language: str | None) -> str:
        base = settings.conversation_llm_base_url
        if not base:
            return text  # no LLM configured -> raw transcript
        headers = {"Authorization": f"Bearer {settings.conversation_llm_api_key}"} if settings.conversation_llm_api_key else {}
        lang = language or "the same language as the transcript"
        messages = [
            {"role": "system", "content": settings.stt_enhance_prompt},
            {"role": "user", "content": f"Language: {lang}\nTranscript: {text}"},
        ]
        try:
            async with httpx.AsyncClient(timeout=settings.stt_enhance_timeout_seconds) as client:
```
New (only the `conversation_llm_*` reads change; `stt_enhance_prompt`/`stt_enhance_timeout_seconds` stay as `settings.*` until Task 4):
```python
    async def _refine(self, text: str, language: str | None) -> str:
        cl = system_config_store.get().conversation_llm
        base = cl.conversation_llm_base_url
        if not base:
            return text  # no LLM configured -> raw transcript
        headers = {"Authorization": f"Bearer {cl.conversation_llm_api_key}"} if cl.conversation_llm_api_key else {}
        lang = language or "the same language as the transcript"
        messages = [
            {"role": "system", "content": settings.stt_enhance_prompt},
            {"role": "user", "content": f"Language: {lang}\nTranscript: {text}"},
        ]
        try:
            async with httpx.AsyncClient(timeout=settings.stt_enhance_timeout_seconds) as client:
```
Add `from app.services.system_config import system_config_store` to this file's imports.

- [ ] **Step 6: `memory/compactor.py`, `embedder.py`, `extractor.py` — `conversation_llm_timeout_seconds`**

In each of the 3 files, replace `timeout=settings.conversation_llm_timeout_seconds` with `timeout=system_config_store.get().conversation_llm.conversation_llm_timeout_seconds`, adding the import.

- [ ] **Step 7: `session.py` — VadEndpointer construction, fast-STT routing, tts_lookahead, opus pacing**

```python
        self.endpointer = VadEndpointer(
            cfg.sample_rate,
            silence_ms=settings.conversation_silence_ms,
            min_speech_ms=settings.conversation_min_speech_ms,
            rms_threshold=settings.conversation_rms_threshold,
            max_utterance_ms=settings.conversation_max_utterance_ms,
            min_silence_ms=settings.conversation_min_silence_ms,
            adaptive_full_ms=settings.conversation_adaptive_full_ms,
            preroll_ms=settings.conversation_preroll_ms,
        )
```
→
```python
        conv_cfg = system_config_store.get().conversation
        self.endpointer = VadEndpointer(
            cfg.sample_rate,
            silence_ms=conv_cfg.conversation_silence_ms,
            min_speech_ms=conv_cfg.conversation_min_speech_ms,
            rms_threshold=conv_cfg.conversation_rms_threshold,
            max_utterance_ms=conv_cfg.conversation_max_utterance_ms,
            min_silence_ms=conv_cfg.conversation_min_silence_ms,
            adaptive_full_ms=conv_cfg.conversation_adaptive_full_ms,
            preroll_ms=conv_cfg.conversation_preroll_ms,
        )
```

```python
        if speech_ms and settings.conversation_fast_stt_engine:
            chosen = select_stt_engine(
                speech_ms,
                cfg.stt_engine,
                settings.conversation_fast_stt_engine,
                settings.conversation_fast_stt_max_ms,
            )
```
→
```python
        conv_cfg = system_config_store.get().conversation
        if speech_ms and conv_cfg.conversation_fast_stt_engine:
            chosen = select_stt_engine(
                speech_ms,
                cfg.stt_engine,
                conv_cfg.conversation_fast_stt_engine,
                conv_cfg.conversation_fast_stt_max_ms,
            )
```
(If `conv_cfg` is already defined earlier in the same method from the endpointer-construction edit above, reuse that local variable instead of re-fetching — check whether these two edits land in the same method or different ones before duplicating the `system_config_store.get().conversation` call; per the research they're in `start()` and `_run_turn()` respectively, two different methods, so each needs its own local fetch.)

```python
                prefetch_synthesis(
                    sentence_aiter, _synth, lookahead=settings.conversation_tts_lookahead
                )
```
→
```python
                prefetch_synthesis(
                    sentence_aiter, _synth,
                    lookahead=system_config_store.get().conversation.conversation_tts_lookahead,
                )
```

```python
                        if settings.conversation_opus_pace and packets:
                            frame_s = self.opus_encoder.frame / self.opus_encoder.sample_rate
                            delays = pacing_delays(
                                len(packets), settings.conversation_opus_prebuffer_frames, frame_s
                            )
```
→
```python
                        conv_cfg = system_config_store.get().conversation
                        if conv_cfg.conversation_opus_pace and packets:
                            frame_s = self.opus_encoder.frame / self.opus_encoder.sample_rate
                            delays = pacing_delays(
                                len(packets), conv_cfg.conversation_opus_prebuffer_frames, frame_s
                            )
```

```python
                delays = pacing_delays(len(packets), settings.conversation_opus_prebuffer_frames, frame_s)
```
(inside `speak()`, unconditional pacing) →
```python
                delays = pacing_delays(
                    len(packets),
                    system_config_store.get().conversation.conversation_opus_prebuffer_frames,
                    frame_s,
                )
```
Add `from app.services.system_config import system_config_store` to `session.py`'s imports (confirm not already present).

- [ ] **Step 8: `livehost.py` — duplicate VadEndpointer, tts_lookahead, opus pacing, and the `conversation_tts_engine` half of the Task-2 or-chain**

Apply the identical transformations as Step 7 to `livehost.py`'s independent copies at the lines identified in research (VadEndpointer ~162-171, tts_lookahead ~275, opus pacing ~287-289). Also finish the or-chain from Task 2 Step 4:
```python
        tts_engine = (
            q.get("tts_engine")
            or settings.conversation_tts_engine
            or system_config_store.get().engines.default_tts_engine
        )
```
→
```python
        conv_cfg = system_config_store.get().conversation
        tts_engine = (
            q.get("tts_engine")
            or conv_cfg.conversation_tts_engine
            or system_config_store.get().engines.default_tts_engine
        )
```

- [ ] **Step 9: `lugo.py` — `conversation_goodbye_text`, and the `conversation_tts_engine` half of the Task-2 or-chain**

```python
                    if settings.conversation_goodbye_text:
                        await session.speak(settings.conversation_goodbye_text)
                        await asyncio.sleep(0.5)
```
→
```python
                    goodbye_text = system_config_store.get().conversation.conversation_goodbye_text
                    if goodbye_text:
                        await session.speak(goodbye_text)
                        await asyncio.sleep(0.5)
```
(Both reads consolidated into one local var, per the research's explicit note that these two reads must move together.)

```python
        tts = dict(
            engine=settings.conversation_tts_engine or system_config_store.get().engines.default_tts_engine,
```
→
```python
        conv_cfg = system_config_store.get().conversation
        tts = dict(
            engine=conv_cfg.conversation_tts_engine or system_config_store.get().engines.default_tts_engine,
```

- [ ] **Step 10: `conversation.py` — `conversation_system_prompt` (2nd site) and `conversation_tts_engine` half**

```python
    system_prompt = inject_memories(system_prompt or settings.conversation_system_prompt, block) if block else system_prompt
```
→
```python
    system_prompt = (
        inject_memories(
            system_prompt or system_config_store.get().conversation.conversation_system_prompt, block
        )
        if block
        else system_prompt
    )
```

```python
        tts_engine = (
            q.get("tts_engine")
            or settings.conversation_tts_engine
            or system_config_store.get().engines.default_tts_engine
        )
```
→
```python
        conv_cfg = system_config_store.get().conversation
        tts_engine = (
            q.get("tts_engine")
            or conv_cfg.conversation_tts_engine
            or system_config_store.get().engines.default_tts_engine
        )
```

- [ ] **Step 11: `profile.py` — `conversation_stt_engine` and `conversation_language`**

```python
    engine = (
        q_engine
        or (getattr(stt_cfg, "engine", "") or None)
        or preset_engine
        or settings.conversation_stt_engine
        or system_config_store.get().engines.default_stt_engine
    )
    if q_language:
        language: str | None = q_language
    elif getattr(stt_cfg, "language", ""):
        language = stt_cfg.language
    elif preset:
        language = preset_lang  # may be None (auto-detect) — authoritative
    else:
        language = settings.conversation_language or None
```
→
```python
    conv_cfg = system_config_store.get().conversation
    engine = (
        q_engine
        or (getattr(stt_cfg, "engine", "") or None)
        or preset_engine
        or conv_cfg.conversation_stt_engine
        or system_config_store.get().engines.default_stt_engine
    )
    if q_language:
        language: str | None = q_language
    elif getattr(stt_cfg, "language", ""):
        language = stt_cfg.language
    elif preset:
        language = preset_lang  # may be None (auto-detect) — authoritative
    else:
        language = conv_cfg.conversation_language or None
```

- [ ] **Step 12: `system_config.py` — finish the two leftover `settings.*` reads inside `warmup_stt_engines()`/`warmup_tts_engines()` from Task 2**

```python
def warmup_stt_engines() -> list[str]:
    engines = system_config_store.get().engines
    extra = [e.strip() for e in engines.extra_warmup_stt_engines.split(",") if e.strip()]
    seen: list[str] = []
    for engine in [settings.conversation_stt_engine, *extra]:
        if engine and engine not in seen:
            seen.append(engine)
    return seen
```
→
```python
def warmup_stt_engines() -> list[str]:
    config = system_config_store.get()
    extra = [e.strip() for e in config.engines.extra_warmup_stt_engines.split(",") if e.strip()]
    seen: list[str] = []
    for engine in [config.conversation.conversation_stt_engine, *extra]:
        if engine and engine not in seen:
            seen.append(engine)
    return seen
```
Apply the mirror change to `warmup_tts_engines()`. `settings` import in this file may now be unused by these two functions — check whether other code in the file (the `SystemConfigStore._resolve_path` method) still needs it before removing the import (it does, per `settings_attr` resolution — keep the import).

- [ ] **Step 13: Delete migrated fields from `Settings`**

Delete from `settings.py`: `conversation_llm_base_url`, `conversation_llm_api_key`, `conversation_llm_model`, `conversation_llm_timeout_seconds`, `ollama_bin`, `conversation_silence_ms`, `conversation_min_silence_ms`, `conversation_adaptive_full_ms`, `conversation_min_speech_ms`, `conversation_rms_threshold`, `conversation_preroll_ms`, `conversation_max_utterance_ms`, `conversation_goodbye_text`, `conversation_stt_engine`, `conversation_fast_stt_engine`, `conversation_fast_stt_max_ms`, `conversation_streaming_stt`, `conversation_streaming_chunk_ms`, `conversation_tts_engine`, `conversation_tts_lookahead`, `conversation_opus_pace`, `conversation_opus_prebuffer_frames`, `conversation_language`, `conversation_system_prompt`.

(`conversation_streaming_stt`/`conversation_streaming_chunk_ms` have no consumer to rewire per the research — they're simply deleted from `Settings`; they already exist with matching defaults in `ConversationTuningConfig` from Task 1, satisfying "field exists in the new store" without any call-site step.)

- [ ] **Step 14: Update existing tests**

Grep `tests/` for every field name deleted in Step 13 plus `settings.ollama_bin`/`settings.conversation_llm_*` and update each to the `system_config_store` fixture pattern (see Task 2 Step 9 for the pattern). Pay particular attention to tests for `llm_models.py`, `responder.py`'s `build_responder`/`resolve_system_prompt`, and `session.py`'s endpointer construction — these are the highest-traffic sites.

- [ ] **Step 15: Run the full test suite**

Run: `pytest`
Expected: PASS.

- [ ] **Step 16: Commit**

```bash
git add apps/api_gateway/app/services/llm_models.py apps/api_gateway/app/services/conversation/responder.py \
  apps/api_gateway/app/services/recommend/service.py apps/api_gateway/app/services/recommend/capabilities.py \
  apps/api_gateway/app/services/stt/providers/whisper_gemma_provider.py \
  apps/api_gateway/app/services/memory/compactor.py apps/api_gateway/app/services/memory/embedder.py \
  apps/api_gateway/app/services/memory/extractor.py apps/api_gateway/app/services/conversation/session.py \
  apps/api_gateway/app/api/routes/livehost.py apps/api_gateway/app/api/routes/lugo.py \
  apps/api_gateway/app/api/routes/conversation.py apps/api_gateway/app/services/stt/profile.py \
  apps/api_gateway/app/services/system_config.py apps/api_gateway/app/core/settings.py tests/
git commit -m "feat(system-config): migrate conversation LLM and conversation-tuning settings off .env"
```

---

### Task 4: STT Local / Whisper / Qwen3 migration

**Files:**
- Modify: `apps/api_gateway/app/services/models.py` (`stt_model_dir`, `vosk_model_base_url`)
- Modify: `apps/api_gateway/app/services/recommend/capabilities.py:155` (`stt_model_dir`, inside `detect_capabilities()`)
- Modify: `apps/api_gateway/app/services/stt/providers/vosk_provider.py` (`vosk_model_path`)
- Modify: `apps/api_gateway/app/services/stt/providers/whisper_provider.py` (`whisper_local_model/device/compute_type`, `whisper_vad_filter`, `whisper_beam_size`, `whisper_condition_on_previous_text`, `whisper_initial_prompt`, `stt_glossary_path`)
- Modify: `apps/api_gateway/app/services/whisper_models.py:117-119` (`whisper_local_device`/`compute_type` in `_warm`)
- Modify: `apps/api_gateway/app/api/routes/system.py:68,77` (`whisper_local_device`, `stt_stream_sample_rate` — status/diagnostics dict)
- Modify: `apps/api_gateway/app/api/routes/lugo.py:107,109`, `apps/api_gateway/app/api/routes/conversation.py:214`, `apps/api_gateway/app/api/routes/livehost.py:119`, `apps/api_gateway/app/api/routes/stt.py:163` (`stt_stream_sample_rate`)
- Modify: `apps/api_gateway/app/services/stt/providers/whisper_mlx_provider.py` (`whisper_mlx_model_path`, `whisper_condition_on_previous_text`, `whisper_initial_prompt`, `stt_glossary_path`)
- Modify: `apps/api_gateway/app/services/stt/providers/qwen3_asr_provider.py:42-43,138` (`qwen3_asr_model`, `qwen3_asr_device`) — and add `clear_qwen3_asr_model_cache()`
- Modify: `apps/api_gateway/app/services/stt/providers/whisper_gemma_provider.py:38,42` (`stt_enhance_prompt`, `stt_enhance_timeout_seconds` — the two fields left over from Task 3)
- Modify: `apps/api_gateway/app/services/stt/profile.py` (`stt_profile` — the field left over from Task 2/3's touches to `resolve_stt`)
- Modify: `apps/api_gateway/app/api/routes/stt.py:69,71,78` (`stt_segment_long_enabled`, `stt_segment_min_seconds`, `stt_segment_concurrency`)
- Modify: `apps/api_gateway/app/core/settings.py` (delete all migrated fields)
- Test: `tests/unit/test_whisper_provider.py`, `test_qwen3_asr_provider.py`, `test_vosk_provider.py`, `test_stt_routes.py` (grep to confirm exact filenames)

**Interfaces:**
- Consumes: `system_config_store.get().stt_local` (from Task 1).
- Produces: `apps/api_gateway/app/services/stt/providers/qwen3_asr_provider.py` gains `clear_model_cache() -> None` (module-level function, clears `_MODEL_CACHE`) — consumed by the cache-invalidation dispatcher added in Task 1's route (wired up in this task's final step).

- [ ] **Step 1: Write a failing test locking down the current (stale-on-device-change) qwen3_asr cache behavior, then a test for the new clear function**

Add to `tests/unit/test_qwen3_asr_provider.py` (confirm exact path first):
```python
def test_clear_model_cache_empties_the_cache(monkeypatch):
    from app.services.stt.providers import qwen3_asr_provider as mod

    mod._MODEL_CACHE["cuda:Qwen/Qwen3-ASR-0.6B"] = object()
    mod.clear_model_cache()
    assert mod._MODEL_CACHE == {}
```

- [ ] **Step 2: Run test, verify it fails**

Run: `pytest tests/unit/test_qwen3_asr_provider.py::test_clear_model_cache_empties_the_cache -v`
Expected: FAIL — `AttributeError: module '...qwen3_asr_provider' has no attribute 'clear_model_cache'`.

- [ ] **Step 3: Implement `clear_model_cache()` and rewire `qwen3_asr_provider.py`'s two settings reads**

```python
def get_active_qwen3_asr_model() -> str:
    return _active_model or settings.qwen3_asr_model
```
→
```python
def get_active_qwen3_asr_model() -> str:
    return _active_model or system_config_store.get().stt_local.qwen3_asr_model
```

```python
    def _cuda_model(self, model: str | None = None):
        resolved = resolve_qwen3_asr_model(model or get_active_qwen3_asr_model())
        key = f"cuda:{resolved}"
        if key not in _MODEL_CACHE:
            import torch
            from qwen_asr import Qwen3ASRModel

            _MODEL_CACHE[key] = Qwen3ASRModel.from_pretrained(
                resolved,
                dtype=_cuda_dtype(torch),
                device_map=settings.qwen3_asr_device or "cuda:0",
                max_new_tokens=256,
            )
        return _MODEL_CACHE[key]
```
→
```python
    def _cuda_model(self, model: str | None = None):
        resolved = resolve_qwen3_asr_model(model or get_active_qwen3_asr_model())
        key = f"cuda:{resolved}"
        if key not in _MODEL_CACHE:
            import torch
            from qwen_asr import Qwen3ASRModel

            _MODEL_CACHE[key] = Qwen3ASRModel.from_pretrained(
                resolved,
                dtype=_cuda_dtype(torch),
                device_map=system_config_store.get().stt_local.qwen3_asr_device or "cuda:0",
                max_new_tokens=256,
            )
        return _MODEL_CACHE[key]
```

Add module-level function (after `set_active_qwen3_asr_model`):
```python
def clear_model_cache() -> None:
    """Drop every cached model instance so the next call rebuilds with current
    settings (e.g. a changed qwen3_asr_device, which the cache key does not
    include — see _cuda_model's key format)."""
    _MODEL_CACHE.clear()
```
Add `from app.services.system_config import system_config_store` to imports; `settings` import may become unused in this file — check other reads before removing (none remain per the research, both call sites migrate here — remove the now-dead `from app.core.settings import settings` import).

- [ ] **Step 4: Run test, verify it passes**

Run: `pytest tests/unit/test_qwen3_asr_provider.py -v`
Expected: PASS.

- [ ] **Step 5: `whisper_provider.py` — rewire `_cache_key`, `_load_model`, `_do_transcribe`, `get_active_whisper_model`**

```python
def get_active_whisper_model() -> str:
    return _active_model or settings.whisper_local_model
```
→
```python
def get_active_whisper_model() -> str:
    return _active_model or system_config_store.get().stt_local.whisper_local_model
```

```python
    def _cache_key(self, model: str) -> str:
        return ":".join(
            [model, settings.whisper_local_device, settings.whisper_local_compute_type]
        )

    def _load_model(self, model: str | None = None):
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "faster-whisper is not installed. Run scripts/setup_local_stt.sh"
            ) from exc

        model_name = model or get_active_whisper_model()
        key = self._cache_key(model_name)
        if key not in _MODEL_CACHE:
            with _MODEL_LOCK:
                if key not in _MODEL_CACHE:
                    _MODEL_CACHE[key] = WhisperModel(
                        resolve_whisper_model(model_name),
                        device=settings.whisper_local_device,
                        compute_type=settings.whisper_local_compute_type,
                    )
        return _MODEL_CACHE[key]
```
→
```python
    def _cache_key(self, model: str) -> str:
        stt_local = system_config_store.get().stt_local
        return ":".join([model, stt_local.whisper_local_device, stt_local.whisper_local_compute_type])

    def _load_model(self, model: str | None = None):
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "faster-whisper is not installed. Run scripts/setup_local_stt.sh"
            ) from exc

        model_name = model or get_active_whisper_model()
        key = self._cache_key(model_name)
        if key not in _MODEL_CACHE:
            with _MODEL_LOCK:
                if key not in _MODEL_CACHE:
                    stt_local = system_config_store.get().stt_local
                    _MODEL_CACHE[key] = WhisperModel(
                        resolve_whisper_model(model_name),
                        device=stt_local.whisper_local_device,
                        compute_type=stt_local.whisper_local_compute_type,
                    )
        return _MODEL_CACHE[key]
```

```python
            segments, _ = whisper_model.transcribe(
                temp_file_path,
                language=language,
                vad_filter=settings.whisper_vad_filter,
                beam_size=settings.whisper_beam_size,
                condition_on_previous_text=settings.whisper_condition_on_previous_text,
                initial_prompt=resolve_initial_prompt(
                    settings.whisper_initial_prompt, settings.stt_glossary_path
                ),
            )
```
→
```python
            stt_local = system_config_store.get().stt_local
            segments, _ = whisper_model.transcribe(
                temp_file_path,
                language=language,
                vad_filter=stt_local.whisper_vad_filter,
                beam_size=stt_local.whisper_beam_size,
                condition_on_previous_text=stt_local.whisper_condition_on_previous_text,
                initial_prompt=resolve_initial_prompt(
                    stt_local.whisper_initial_prompt, stt_local.stt_glossary_path
                ),
            )
```
Add the `system_config_store` import; remove the now-unused `settings` import from this file (confirm no other reads remain).

- [ ] **Step 6: `whisper_models.py:117-119` — `_warm`**

```python
        WhisperModel(
            resolve_whisper_model(size),
            device=settings.whisper_local_device,
            compute_type=settings.whisper_local_compute_type,
        )
```
→
```python
        stt_local = system_config_store.get().stt_local
        WhisperModel(
            resolve_whisper_model(size),
            device=stt_local.whisper_local_device,
            compute_type=stt_local.whisper_local_compute_type,
        )
```

- [ ] **Step 7: `whisper_mlx_provider.py` — `whisper_mlx_model_path`, `whisper_condition_on_previous_text`, `whisper_initial_prompt`, `stt_glossary_path`**

```python
    def available(self) -> bool:
        try:
            import mlx_whisper  # noqa: F401
        except ImportError:
            return False
        return os.path.isdir(settings.whisper_mlx_model_path)

    def detail(self) -> str:
        return f"{os.path.basename(settings.whisper_mlx_model_path)} · Apple GPU (MLX)"

    def _transcribe(self, wav_path: str, language: str | None) -> str:
        import mlx_whisper

        result = mlx_whisper.transcribe(
            wav_path,
            path_or_hf_repo=settings.whisper_mlx_model_path,
            language=language,
            condition_on_previous_text=settings.whisper_condition_on_previous_text,
            initial_prompt=resolve_initial_prompt(
                settings.whisper_initial_prompt, settings.stt_glossary_path
            ),
        )
        return (result.get("text") or "").strip()
```
→
```python
    def available(self) -> bool:
        try:
            import mlx_whisper  # noqa: F401
        except ImportError:
            return False
        return os.path.isdir(system_config_store.get().stt_local.whisper_mlx_model_path)

    def detail(self) -> str:
        path = system_config_store.get().stt_local.whisper_mlx_model_path
        return f"{os.path.basename(path)} · Apple GPU (MLX)"

    def _transcribe(self, wav_path: str, language: str | None) -> str:
        import mlx_whisper

        stt_local = system_config_store.get().stt_local
        result = mlx_whisper.transcribe(
            wav_path,
            path_or_hf_repo=stt_local.whisper_mlx_model_path,
            language=language,
            condition_on_previous_text=stt_local.whisper_condition_on_previous_text,
            initial_prompt=resolve_initial_prompt(
                stt_local.whisper_initial_prompt, stt_local.stt_glossary_path
            ),
        )
        return (result.get("text") or "").strip()
```
Add the `system_config_store` import; remove `settings` if now unused.

- [ ] **Step 8: `whisper_gemma_provider.py` — the 2 remaining fields (`stt_enhance_prompt`, `stt_enhance_timeout_seconds`)**

```python
        headers = {"Authorization": f"Bearer {cl.conversation_llm_api_key}"} if cl.conversation_llm_api_key else {}
        lang = language or "the same language as the transcript"
        messages = [
            {"role": "system", "content": settings.stt_enhance_prompt},
            {"role": "user", "content": f"Language: {lang}\nTranscript: {text}"},
        ]
        try:
            async with httpx.AsyncClient(timeout=settings.stt_enhance_timeout_seconds) as client:
```
→
```python
        headers = {"Authorization": f"Bearer {cl.conversation_llm_api_key}"} if cl.conversation_llm_api_key else {}
        lang = language or "the same language as the transcript"
        stt_local = system_config_store.get().stt_local
        messages = [
            {"role": "system", "content": stt_local.stt_enhance_prompt},
            {"role": "user", "content": f"Language: {lang}\nTranscript: {text}"},
        ]
        try:
            async with httpx.AsyncClient(timeout=stt_local.stt_enhance_timeout_seconds) as client:
```
`settings` import in this file is now fully unused — remove it (confirm no other reads first).

- [ ] **Step 9: `models.py` (`ModelManager`) — `stt_model_dir`, `vosk_model_base_url`**

```python
class ModelManager:
    def __init__(self) -> None:
        self._base = Path(settings.stt_model_dir)
        self._base.mkdir(parents=True, exist_ok=True)
```
→
```python
class ModelManager:
    def __init__(self) -> None:
        self._base = Path(system_config_store.get().stt_local.stt_model_dir)
        self._base.mkdir(parents=True, exist_ok=True)
```
Note: `model_manager = ModelManager()` is a module-level singleton constructed at import time — `system_config_store.get()` must be reachable at that point (it is; `system_config_store` is itself a module-level singleton with lazy `_ensure()`, safe to call at any import order). This bakes `stt_model_dir` in at process start, same caching behavior as today (`settings.stt_model_dir` was equally baked in before) — no regression, not a target for the cache-invalidation hooks list.

```python
            url = f"{settings.vosk_model_base_url.rstrip('/')}/{name}.zip"
```
→
```python
            url = f"{system_config_store.get().stt_local.vosk_model_base_url.rstrip('/')}/{name}.zip"
```

- [ ] **Step 10: `recommend/capabilities.py` — `stt_model_dir`**

The enclosing function is `detect_capabilities()` (`apps/api_gateway/app/services/recommend/capabilities.py:129-161`). Replace:
```python
def detect_capabilities() -> Capabilities:
    ...
    return Capabilities(
        os=sys_os,
        arch=arch,
        apple_silicon=apple_silicon,
        cpu_count=os.cpu_count(),
        ram_total_gb=_ram_total_gb(),
        disk_free_gb=_disk_free_gb(settings.stt_model_dir),
        mlx=mlx,
        cuda=_cuda(),
        libopus=_libopus(),
        ollama=_ollama(),
        modules=modules,
    )
```
→
```python
def detect_capabilities() -> Capabilities:
    ...
    return Capabilities(
        os=sys_os,
        arch=arch,
        apple_silicon=apple_silicon,
        cpu_count=os.cpu_count(),
        ram_total_gb=_ram_total_gb(),
        disk_free_gb=_disk_free_gb(system_config_store.get().stt_local.stt_model_dir),
        mlx=mlx,
        cuda=_cuda(),
        libopus=_libopus(),
        ollama=_ollama(),
        modules=modules,
    )
```
(`...` denotes the unchanged `sys_os`/`arch`/`apple_silicon`/`modules`/`mlx` lines above the `return` — only the `disk_free_gb` argument changes.)

- [ ] **Step 11: `vosk_provider.py` — `vosk_model_path`**

```python
def get_active_vosk_path() -> str:
    return _active_path or settings.vosk_model_path
```
→
```python
def get_active_vosk_path() -> str:
    return _active_path or system_config_store.get().stt_local.vosk_model_path
```

- [ ] **Step 12: `stt_stream_sample_rate` — 6 sites across `system.py`, `lugo.py`, `conversation.py`, `livehost.py`, `stt.py`**

`system.py`:
```python
        "stream_sample_rate": settings.stt_stream_sample_rate,
```
→
```python
        "stream_sample_rate": system_config_store.get().stt_local.stt_stream_sample_rate,
```
(Also fix `"device": settings.whisper_local_device,` on the adjacent line in the same dict → `"device": system_config_store.get().stt_local.whisper_local_device,`.)

`lugo.py`:
```python
    try:
        in_sr = int((hello.get("audio_params") or {}).get("sample_rate", settings.stt_stream_sample_rate))
    except (TypeError, ValueError):
        in_sr = settings.stt_stream_sample_rate
```
→
```python
    default_sample_rate = system_config_store.get().stt_local.stt_stream_sample_rate
    try:
        in_sr = int((hello.get("audio_params") or {}).get("sample_rate", default_sample_rate))
    except (TypeError, ValueError):
        in_sr = default_sample_rate
```

`conversation.py`, `livehost.py` (identical pattern in both):
```python
    sample_rate = int(q.get("sample_rate", settings.stt_stream_sample_rate))
```
→
```python
    sample_rate = int(q.get("sample_rate", system_config_store.get().stt_local.stt_stream_sample_rate))
```

`stt.py` (`stt_stream`):
```python
    sample_rate = int(
        websocket.query_params.get("sample_rate", settings.stt_stream_sample_rate)
    )
```
→
```python
    sample_rate = int(
        websocket.query_params.get("sample_rate", system_config_store.get().stt_local.stt_stream_sample_rate)
    )
```

- [ ] **Step 13: `stt.py` — `stt_segment_long_enabled`, `stt_segment_min_seconds`, `stt_segment_concurrency`**

```python
    use_segment = _resolve_flag(segment, settings.stt_segment_long_enabled)
    try:
        if use_segment and wav_duration_seconds(audio_bytes) >= settings.stt_segment_min_seconds:
            pcm, sample_rate, _, _ = read_wav(audio_bytes)
            result = await transcribe_long(
                provider,
                pcm16_to_float_array(pcm),
                sample_rate,
                language=payload.language,
                concurrency=settings.stt_segment_concurrency,
            )
```
→
```python
    stt_local = system_config_store.get().stt_local
    use_segment = _resolve_flag(segment, stt_local.stt_segment_long_enabled)
    try:
        if use_segment and wav_duration_seconds(audio_bytes) >= stt_local.stt_segment_min_seconds:
            pcm, sample_rate, _, _ = read_wav(audio_bytes)
            result = await transcribe_long(
                provider,
                pcm16_to_float_array(pcm),
                sample_rate,
                language=payload.language,
                concurrency=stt_local.stt_segment_concurrency,
            )
```

- [ ] **Step 14: `profile.py` — `stt_profile`**

```python
    from app.core.settings import settings
    from app.services.system_config import system_config_store

    stt_cfg = getattr(profile, "stt", None)
    preset_name = (getattr(stt_cfg, "profile", "") or "") or settings.stt_profile
```
→
```python
    from app.services.system_config import system_config_store

    stt_cfg = getattr(profile, "stt", None)
    preset_name = (getattr(stt_cfg, "profile", "") or "") or system_config_store.get().stt_local.stt_profile
```
(`settings` import in `resolve_stt`'s local-import line can now be removed entirely — all its reads in this function were migrated across Tasks 2, 3, and this task.)

- [ ] **Step 15: Delete migrated fields from `Settings`**

Delete from `settings.py`: `stt_model_dir`, `vosk_model_path`, `vosk_model_base_url`, `stt_stream_sample_rate`, `whisper_local_model`, `whisper_local_device`, `whisper_local_compute_type`, `whisper_vad_filter`, `whisper_beam_size`, `whisper_condition_on_previous_text`, `whisper_initial_prompt`, `stt_glossary_path`, `stt_profile`, `whisper_mlx_model_path`, `qwen3_asr_model`, `qwen3_asr_device`, `stt_enhance_timeout_seconds`, `stt_enhance_prompt`, `stt_segment_long_enabled`, `stt_segment_min_seconds`, `stt_segment_concurrency`.

- [ ] **Step 16: Wire `clear_qwen3_asr_model_cache` (rename note: Step 3 named it `clear_model_cache` inside the module — the dispatcher imports it under an explicit alias) into the Task-1 cache-invalidation marker**

In `apps/api_gateway/app/api/routes/system.py`, replace the marker comment from Task 1 Step 7 with the first real hook:
```python
    new_config = system_config_store.set(merged)
    if current.stt_local.qwen3_asr_device != new_config.stt_local.qwen3_asr_device:
        from app.services.stt.providers.qwen3_asr_provider import clear_model_cache

        clear_model_cache()
    # Remaining cache-invalidation hooks (preprocessing.pyannote_*, remote_stt, omnivoice)
    # are added here incrementally in Tasks 5, 6, 7.
    return {"success": True, "data": _mask_system_config(new_config)}
```

- [ ] **Step 17: Write a test for the new invalidation branch**

Add to `tests/unit/test_system_config_routes.py`:
```python
def test_changing_qwen3_asr_device_clears_the_model_cache(client, monkeypatch):
    from app.services.stt.providers import qwen3_asr_provider as mod

    mod._MODEL_CACHE["cuda:some-model"] = object()
    full = client.get("/v1/system/config").json()["data"]
    full["stt_local"]["qwen3_asr_device"] = "cuda:1"
    client.put("/v1/system/config", json=full)
    assert mod._MODEL_CACHE == {}


def test_unrelated_field_change_does_not_clear_qwen3_asr_cache(client):
    from app.services.stt.providers import qwen3_asr_provider as mod

    sentinel = object()
    mod._MODEL_CACHE["cuda:some-model"] = sentinel
    full = client.get("/v1/system/config").json()["data"]
    full["base_context"] = "unrelated change"
    client.put("/v1/system/config", json=full)
    assert mod._MODEL_CACHE.get("cuda:some-model") is sentinel
```

- [ ] **Step 18: Run test, verify it fails then passes**

Run: `pytest tests/unit/test_system_config_routes.py -k qwen3_asr -v`
Expected: FAIL before Step 16's edit lands (it won't exist as a route behavior), PASS after.

- [ ] **Step 19: Update existing tests for every field/site touched in this task**

Grep `tests/` for each field name deleted in Step 15 and update to the `system_config_store` fixture pattern.

- [ ] **Step 20: Run the full test suite**

Run: `pytest`
Expected: PASS.

- [ ] **Step 21: Commit**

```bash
git add apps/api_gateway/app/services/models.py apps/api_gateway/app/services/recommend/capabilities.py \
  apps/api_gateway/app/services/stt/providers/vosk_provider.py \
  apps/api_gateway/app/services/stt/providers/whisper_provider.py \
  apps/api_gateway/app/services/whisper_models.py apps/api_gateway/app/api/routes/system.py \
  apps/api_gateway/app/api/routes/lugo.py apps/api_gateway/app/api/routes/conversation.py \
  apps/api_gateway/app/api/routes/livehost.py apps/api_gateway/app/api/routes/stt.py \
  apps/api_gateway/app/services/stt/providers/whisper_mlx_provider.py \
  apps/api_gateway/app/services/stt/providers/qwen3_asr_provider.py \
  apps/api_gateway/app/services/stt/providers/whisper_gemma_provider.py \
  apps/api_gateway/app/services/stt/profile.py apps/api_gateway/app/core/settings.py tests/
git commit -m "feat(system-config): migrate STT local/Whisper/Qwen3 settings off .env, add qwen3_asr cache invalidation"
```

---

### Task 5: Preprocessing migration

**Files:**
- Modify: `apps/api_gateway/app/api/routes/system.py:79-83` (status dict — 4 fields)
- Modify: `apps/api_gateway/app/api/routes/stt.py:58-65,166-170,216-219` (`transcribe`, `stt_stream`)
- Modify: `apps/api_gateway/app/api/routes/conversation.py:231`
- Modify: `apps/api_gateway/app/services/conversation/session.py` (the `stt_noise_reduce_amount` site left over from Task 3)
- Modify: `apps/api_gateway/app/services/vad.py` (pyannote cache — add `clear_pyannote_cache()`)
- Modify: `apps/api_gateway/app/core/settings.py` (delete migrated fields)
- Modify: `apps/api_gateway/app/api/routes/system.py` (wire the 2nd cache-invalidation hook)
- Test: `tests/unit/test_vad.py` (grep to confirm), `tests/unit/test_system_config_routes.py`

**Interfaces:**
- Consumes: `system_config_store.get().preprocessing`.
- Produces: `apps/api_gateway/app/services/vad.py` gains `clear_pyannote_cache() -> None`.

- [ ] **Step 1: Write a failing test for `clear_pyannote_cache`**

Add to `tests/unit/test_vad.py` (confirm the exact path first; create it if it doesn't exist):
```python
def test_clear_pyannote_cache_empties_the_cache():
    from app.services import vad as mod

    mod._pyannote_cache["pipeline"] = object()
    mod.clear_pyannote_cache()
    assert mod._pyannote_cache == {}
```

- [ ] **Step 2: Run test, verify it fails**

Run: `pytest tests/unit/test_vad.py::test_clear_pyannote_cache_empties_the_cache -v`
Expected: FAIL — `AttributeError: module '...vad' has no attribute 'clear_pyannote_cache'`.

- [ ] **Step 3: Implement `clear_pyannote_cache()` and rewire `vad.py`'s pyannote settings reads**

`apps/api_gateway/app/services/vad.py` full current content of the two functions that read the migrated fields:
```python
def available_backends() -> dict[str, bool]:
    torch_ok = module_available("torch")
    # pyannote's default VAD pipeline is gated on HF -> needs an auth token.
    pyannote_ok = torch_ok and module_available("pyannote.audio") and bool(settings.pyannote_auth_token)
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
Replace both with:
```python
def available_backends() -> dict[str, bool]:
    torch_ok = module_available("torch")
    # pyannote's default VAD pipeline is gated on HF -> needs an auth token.
    pyannote_ok = (
        torch_ok
        and module_available("pyannote.audio")
        and bool(system_config_store.get().preprocessing.pyannote_auth_token)
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
        preprocessing = system_config_store.get().preprocessing
        token = preprocessing.pyannote_auth_token or True
        # segmentation-3.0 is a Model; wrap it in the VAD pipeline (pyannote 3.1/4.x way).
        model = Model.from_pretrained(preprocessing.pyannote_vad_model, token=token)
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
Add module-level function next to `_pyannote_cache`'s definition (`apps/api_gateway/app/services/vad.py:27`):
```python
def clear_pyannote_cache() -> None:
    """Drop the cached pipeline so the next VAD call rebuilds it with the
    current pyannote_vad_model/pyannote_auth_token."""
    _pyannote_cache.clear()
```
Add `from app.services.system_config import system_config_store` to `vad.py`'s imports. `settings` import becomes unused in this file — remove it (both its reads, lines 33 and 71/73, are migrated above; no other read of `settings` exists in this file per its full content shown above).

- [ ] **Step 4: Run test, verify it passes**

Run: `pytest tests/unit/test_vad.py -v`
Expected: PASS.

- [ ] **Step 5: `system.py` status dict — 4 fields**

```python
        "stt_preprocess": {
            "vad": settings.stt_vad_enabled,
            "vad_backend": settings.stt_vad_backend,
            "vad_backends_available": available_backends(),
            "noise_reduce": settings.stt_noise_reduce_enabled,
            "noise_reduce_amount": settings.stt_noise_reduce_amount,
        },
```
→
```python
        "stt_preprocess": {
            "vad": preprocessing.stt_vad_enabled,
            "vad_backend": preprocessing.stt_vad_backend,
            "vad_backends_available": available_backends(),
            "noise_reduce": preprocessing.stt_noise_reduce_enabled,
            "noise_reduce_amount": preprocessing.stt_noise_reduce_amount,
        },
```
Add `preprocessing = system_config_store.get().preprocessing` once near the top of `system_status`, alongside the other locals already fetched in that function from Task 4 Step 12.

- [ ] **Step 6: `stt.py` — `transcribe`, `stt_stream`, message-loop denoise**

```python
    backend = vad_backend or settings.stt_vad_backend
    audio_bytes = preprocess_wav_bytes(
        audio_bytes,
        denoise=_resolve_flag(denoise, settings.stt_noise_reduce_enabled),
        vad=_resolve_flag(vad, settings.stt_vad_enabled),
        amount=settings.stt_noise_reduce_amount,
        vad_fn=lambda s, sr: apply_vad(s, sr, backend),
    )
```
→
```python
    preprocessing = system_config_store.get().preprocessing
    backend = vad_backend or preprocessing.stt_vad_backend
    audio_bytes = preprocess_wav_bytes(
        audio_bytes,
        denoise=_resolve_flag(denoise, preprocessing.stt_noise_reduce_enabled),
        vad=_resolve_flag(vad, preprocessing.stt_vad_enabled),
        amount=preprocessing.stt_noise_reduce_amount,
        vad_fn=lambda s, sr: apply_vad(s, sr, backend),
    )
```

```python
    denoise = _resolve_flag(
        _parse_bool(websocket.query_params.get("denoise")), settings.stt_noise_reduce_enabled
    )
    vad = _resolve_flag(
        _parse_bool(websocket.query_params.get("vad")), settings.stt_vad_enabled
    )
```
→
```python
    preprocessing = system_config_store.get().preprocessing
    denoise = _resolve_flag(
        _parse_bool(websocket.query_params.get("denoise")), preprocessing.stt_noise_reduce_enabled
    )
    vad = _resolve_flag(
        _parse_bool(websocket.query_params.get("vad")), preprocessing.stt_vad_enabled
    )
```
(This `preprocessing` local is in `stt_stream`, a different function from the one in Step 6's first edit (`transcribe`) — each function needs its own fetch.)

```python
                if denoise or vad:
                    frame = preprocess_pcm16(
                        frame, sample_rate, denoise=denoise, vad=vad,
                        amount=settings.stt_noise_reduce_amount,
                    )
```
→
```python
                if denoise or vad:
                    frame = preprocess_pcm16(
                        frame, sample_rate, denoise=denoise, vad=vad,
                        amount=system_config_store.get().preprocessing.stt_noise_reduce_amount,
                    )
```
(Same `stt_stream` function as the previous edit — reuse the `preprocessing` local already fetched above instead of calling `system_config_store.get()` again.)

- [ ] **Step 7: `conversation.py:231` and `session.py` — `stt_noise_reduce_enabled`/`stt_noise_reduce_amount`**

```python
    denoise = _truthy(q.get("denoise"), settings.stt_noise_reduce_enabled)
```
→
```python
    denoise = _truthy(q.get("denoise"), system_config_store.get().preprocessing.stt_noise_reduce_enabled)
```

`session.py`:
```python
        if cfg.denoise:
            pcm = preprocess_pcm16(
                audio_pcm, cfg.sample_rate, denoise=True, vad=False,
                amount=settings.stt_noise_reduce_amount,
            )
```
→
```python
        if cfg.denoise:
            pcm = preprocess_pcm16(
                audio_pcm, cfg.sample_rate, denoise=True, vad=False,
                amount=system_config_store.get().preprocessing.stt_noise_reduce_amount,
            )
```

- [ ] **Step 8: Delete migrated fields from `Settings`**

Delete from `settings.py`: `stt_vad_enabled`, `stt_vad_backend`, `stt_noise_reduce_enabled`, `stt_noise_reduce_amount`, `pyannote_vad_model`, `pyannote_auth_token`.

- [ ] **Step 9: Wire the pyannote cache-invalidation hook**

In `apps/api_gateway/app/api/routes/system.py`, extend the hook chain from Task 4 Step 16:
```python
    new_config = system_config_store.set(merged)
    if current.stt_local.qwen3_asr_device != new_config.stt_local.qwen3_asr_device:
        from app.services.stt.providers.qwen3_asr_provider import clear_model_cache

        clear_model_cache()
    if (
        current.preprocessing.pyannote_vad_model != new_config.preprocessing.pyannote_vad_model
        or current.preprocessing.pyannote_auth_token != new_config.preprocessing.pyannote_auth_token
    ):
        from app.services.vad import clear_pyannote_cache

        clear_pyannote_cache()
    # Remaining cache-invalidation hooks (remote_stt, omnivoice) added in Tasks 6, 7.
    return {"success": True, "data": _mask_system_config(new_config)}
```

- [ ] **Step 10: Write a test for the new invalidation branch, run it, verify fail then pass**

Add to `tests/unit/test_system_config_routes.py`, mirroring the qwen3 tests from Task 4 Step 17 (change the cache module/attribute names to whatever Step 1 of this task confirmed for `vad.py`). Run: `pytest tests/unit/test_system_config_routes.py -k pyannote -v`, confirm FAIL then PASS.

- [ ] **Step 11: Update existing tests, run full suite, commit**

Grep `tests/` for the 6 fields deleted in Step 8 and update. Run: `pytest` — expect PASS.

```bash
git add apps/api_gateway/app/api/routes/system.py apps/api_gateway/app/api/routes/stt.py \
  apps/api_gateway/app/api/routes/conversation.py apps/api_gateway/app/services/conversation/session.py \
  apps/api_gateway/app/services/vad.py apps/api_gateway/app/core/settings.py tests/
git commit -m "feat(system-config): migrate STT preprocessing settings off .env, add pyannote cache invalidation"
```

---

### Task 6: Remote STT migration

**Files:**
- Modify: `apps/api_gateway/app/services/stt/service.py` (`STTService.__init__`, `list_engines`)
- Modify: `apps/api_gateway/app/services/recommend/service.py:86-87` (`whisper_service_base_url`, `eventlab_base_url` — the 2 fields left from Task 3)
- Modify: `apps/api_gateway/app/core/settings.py` (delete migrated fields)
- Modify: `apps/api_gateway/app/api/routes/system.py` (wire the 3rd cache-invalidation hook)
- Test: `tests/unit/test_stt_service_openrouter.py` (extend), new remote-provider reinit test

**Interfaces:**
- Consumes: `system_config_store.get().remote_stt`.
- Produces: `STTService.reinit_remote_providers(remote_stt: RemoteSttConfig) -> None` — rebuilds and swaps the `whisper_service`/`eventlab` entries in `self.providers`.

- [ ] **Step 1: Write a failing test for `reinit_remote_providers`**

Add to `tests/unit/test_stt_service_openrouter.py` (or a new `tests/unit/test_stt_service_remote.py` if that fits the repo's file-per-concern convention better — check existing naming first):
```python
def test_reinit_remote_providers_rebuilds_whisper_service_and_eventlab():
    from app.services.system_config import RemoteSttConfig
    from app.services.stt.providers.remote_whisper_provider import RemoteWhisperProvider

    svc = STTService()
    original_whisper_service = svc.providers["whisper_service"]
    original_eventlab = svc.providers["eventlab"]

    new_cfg = RemoteSttConfig(
        whisper_service_base_url="https://new-endpoint.example/v1",
        whisper_service_api_key="new-key",
        whisper_service_model="whisper-2",
        eventlab_base_url="https://eventlab.example/v1",
        eventlab_api_key="ev-key",
        eventlab_model="whisper-1",
        remote_stt_timeout_seconds=15.0,
    )
    svc.reinit_remote_providers(new_cfg)

    assert svc.providers["whisper_service"] is not original_whisper_service
    assert isinstance(svc.providers["whisper_service"], RemoteWhisperProvider)
    assert svc.providers["whisper_service"].base_url == "https://new-endpoint.example/v1"
    assert svc.providers["whisper_service"].api_key == "new-key"
    assert svc.providers["whisper_service"].model == "whisper-2"
    assert svc.providers["whisper_service"].timeout_seconds == 15.0

    assert svc.providers["eventlab"] is not original_eventlab
    assert svc.providers["eventlab"].base_url == "https://eventlab.example/v1"
```

- [ ] **Step 2: Run test, verify it fails**

Run: `pytest tests/unit/test_stt_service_openrouter.py::test_reinit_remote_providers_rebuilds_whisper_service_and_eventlab -v`
Expected: FAIL — `AttributeError: 'STTService' object has no attribute 'reinit_remote_providers'`.

- [ ] **Step 3: Implement the rewire and `reinit_remote_providers`**

```python
class STTService:
    def __init__(self) -> None:
        whisper_local = WhisperProvider()
        remote_stt = system_config_store.get().remote_stt
        self.providers: dict[str, STTProvider] = {
            "vosk": VoskProvider(),
            "whisper": whisper_local,
            "whisper_local": whisper_local,
            "whisper_mlx": WhisperMlxProvider(),
            "qwen3_asr": Qwen3AsrProvider(),
            "whisper_gemma": WhisperGemmaProvider(),
            "whisper_service": RemoteWhisperProvider(
                name="whisper_service",
                base_url=remote_stt.whisper_service_base_url,
                api_key=remote_stt.whisper_service_api_key,
                model=remote_stt.whisper_service_model,
                timeout_seconds=remote_stt.remote_stt_timeout_seconds,
            ),
            "eventlab": RemoteWhisperProvider(
                name="eventlab",
                base_url=remote_stt.eventlab_base_url,
                api_key=remote_stt.eventlab_api_key,
                model=remote_stt.eventlab_model,
                timeout_seconds=remote_stt.remote_stt_timeout_seconds,
            ),
            "qwen3_asr_or": OpenRouterSttProvider(
                name="qwen3_asr_or",
                model="qwen/qwen3-asr-flash-2026-02-10",
                timeout_seconds=remote_stt.remote_stt_timeout_seconds,
            ),
            "whisper_or": OpenRouterSttProvider(
                name="whisper_or",
                model="openai/whisper-large-v3-turbo",
                timeout_seconds=remote_stt.remote_stt_timeout_seconds,
            ),
        }

    def reinit_remote_providers(self, remote_stt) -> None:
        """Rebuild whisper_service/eventlab/qwen3_asr_or/whisper_or with fresh
        settings — these providers cache base_url/api_key/model/timeout as
        instance attributes at construction and never re-read afterward."""
        self.providers["whisper_service"] = RemoteWhisperProvider(
            name="whisper_service",
            base_url=remote_stt.whisper_service_base_url,
            api_key=remote_stt.whisper_service_api_key,
            model=remote_stt.whisper_service_model,
            timeout_seconds=remote_stt.remote_stt_timeout_seconds,
        )
        self.providers["eventlab"] = RemoteWhisperProvider(
            name="eventlab",
            base_url=remote_stt.eventlab_base_url,
            api_key=remote_stt.eventlab_api_key,
            model=remote_stt.eventlab_model,
            timeout_seconds=remote_stt.remote_stt_timeout_seconds,
        )
        self.providers["qwen3_asr_or"] = OpenRouterSttProvider(
            name="qwen3_asr_or",
            model="qwen/qwen3-asr-flash-2026-02-10",
            timeout_seconds=remote_stt.remote_stt_timeout_seconds,
        )
        self.providers["whisper_or"] = OpenRouterSttProvider(
            name="whisper_or",
            model="openai/whisper-large-v3-turbo",
            timeout_seconds=remote_stt.remote_stt_timeout_seconds,
        )
```
Also update `list_engines`' `remote` dict:
```python
        remote = {
            "whisper_service": (settings.whisper_service_base_url, settings.whisper_service_model),
            "eventlab": (settings.eventlab_base_url, settings.eventlab_model),
        }
```
→
```python
        remote_stt = system_config_store.get().remote_stt
        remote = {
            "whisper_service": (remote_stt.whisper_service_base_url, remote_stt.whisper_service_model),
            "eventlab": (remote_stt.eventlab_base_url, remote_stt.eventlab_model),
        }
```
Remove the now-unused `from app.core.settings import settings` import from `service.py` if nothing else in the file reads it (confirm first).

- [ ] **Step 4: Run test, verify it passes**

Run: `pytest tests/unit/test_stt_service_openrouter.py -v`
Expected: PASS (including the pre-existing OpenRouter tests, unaffected).

- [ ] **Step 5: `recommend/service.py` — the 2 leftover fields**

```python
def _augment_config_flags(caps: Capabilities) -> None:
    caps.modules["whisper_service"] = bool(settings.whisper_service_base_url)
    caps.modules["eventlab"] = bool(settings.eventlab_base_url)
    caps.modules["online_llm"] = bool(system_config_store.get().conversation_llm.conversation_llm_base_url)
    caps.modules["openrouter"] = bool(system_config_store.get().openrouter_api_key)
```
→
```python
def _augment_config_flags(caps: Capabilities) -> None:
    remote_stt = system_config_store.get().remote_stt
    caps.modules["whisper_service"] = bool(remote_stt.whisper_service_base_url)
    caps.modules["eventlab"] = bool(remote_stt.eventlab_base_url)
    caps.modules["online_llm"] = bool(system_config_store.get().conversation_llm.conversation_llm_base_url)
    caps.modules["openrouter"] = bool(system_config_store.get().openrouter_api_key)
```
`settings` import in this file may now be fully unused — check the rest of the file before removing.

- [ ] **Step 6: Delete migrated fields from `Settings`**

Delete: `whisper_service_base_url`, `whisper_service_api_key`, `whisper_service_model`, `eventlab_base_url`, `eventlab_api_key`, `eventlab_model`, `remote_stt_timeout_seconds`.

- [ ] **Step 7: Wire the `reinit_remote_providers` cache-invalidation hook**

```python
    new_config = system_config_store.set(merged)
    if current.stt_local.qwen3_asr_device != new_config.stt_local.qwen3_asr_device:
        from app.services.stt.providers.qwen3_asr_provider import clear_model_cache

        clear_model_cache()
    if (
        current.preprocessing.pyannote_vad_model != new_config.preprocessing.pyannote_vad_model
        or current.preprocessing.pyannote_auth_token != new_config.preprocessing.pyannote_auth_token
    ):
        from app.services.vad import clear_pyannote_cache

        clear_pyannote_cache()
    if current.remote_stt != new_config.remote_stt:
        from app.services.stt.service import stt_service

        stt_service.reinit_remote_providers(new_config.remote_stt)
    # Remaining cache-invalidation hook (omnivoice) added in Task 7.
    return {"success": True, "data": _mask_system_config(new_config)}
```

- [ ] **Step 8: Write a test for the wired hook, run it, verify fail then pass**

Add to `tests/unit/test_system_config_routes.py`:
```python
def test_changing_remote_stt_base_url_rebuilds_the_provider(client):
    from app.services.stt.service import stt_service

    original = stt_service.providers["whisper_service"]
    full = client.get("/v1/system/config").json()["data"]
    full["remote_stt"]["whisper_service_base_url"] = "https://changed.example/v1"
    client.put("/v1/system/config", json=full)
    assert stt_service.providers["whisper_service"] is not original
    assert stt_service.providers["whisper_service"].base_url == "https://changed.example/v1"
```
Run: `pytest tests/unit/test_system_config_routes.py -k remote_stt -v` — verify FAIL before Step 7, PASS after.

- [ ] **Step 9: Update existing tests, run full suite, commit**

Grep `tests/` for the 7 fields deleted in Step 6 and update.

```bash
git add apps/api_gateway/app/services/stt/service.py apps/api_gateway/app/services/recommend/service.py \
  apps/api_gateway/app/core/settings.py apps/api_gateway/app/api/routes/system.py tests/
git commit -m "feat(system-config): migrate remote STT provider settings off .env, rebuild providers on save"
```

---

### Task 7: OmniVoice migration

**Files:**
- Modify: `apps/api_gateway/app/services/tts/providers/omnivoice_provider.py` (every `settings.omnivoice_*` read, plus sidecar PID tracking so a respawn can kill the old process)
- Modify: `apps/api_gateway/app/services/recommend/capabilities.py:143` (`omnivoice_path`)
- Modify: `apps/api_gateway/app/api/routes/system.py:63-64` (`omnivoice_path` in status dict, and wiring the final cache-invalidation hook)
- Modify: `apps/api_gateway/app/core/settings.py` (delete migrated fields)
- Test: `tests/unit/test_omnivoice_provider.py` (extend)

**Interfaces:**
- Consumes: `system_config_store.get().omnivoice`.
- Produces: `apps/api_gateway/app/services/tts/providers/omnivoice_provider.py` gains `reset_voice_ref_and_respawn() -> None` (module-level) and a module-level `_sidecar_process: subprocess.Popen | None` tracking variable so the old sidecar can be killed before a respawn (none exists today — the research confirmed `_spawn_sidecar()` never retains a handle).

- [ ] **Step 1: Write failing tests for PID tracking and `reset_voice_ref_and_respawn`**

Add to `tests/unit/test_omnivoice_provider.py`:
```python
def test_spawn_sidecar_tracks_the_process_handle(monkeypatch, tmp_path):
    import app.services.tts.providers.omnivoice_provider as ov_mod

    monkeypatch.setattr(ov_mod.settings, "artifacts_dir", str(tmp_path))
    fake_popen_calls = []

    class _FakePopen:
        def __init__(self, *a, **kw):
            fake_popen_calls.append((a, kw))
            self.pid = 12345
            self._killed = False

        def poll(self):
            return None if not self._killed else 0

        def kill(self):
            self._killed = True

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(ov_mod.subprocess, "Popen", _FakePopen)
    ov_mod._sidecar_process = None
    provider = ov_mod.OmniVoiceProvider()
    provider._spawn_sidecar()
    assert ov_mod._sidecar_process is not None
    assert ov_mod._sidecar_process.pid == 12345
    ov_mod._sidecar_process = None  # reset module state for other tests


def test_reset_voice_ref_and_respawn_clears_voice_ref_and_kills_old_sidecar(monkeypatch, tmp_path):
    import app.services.tts.providers.omnivoice_provider as ov_mod

    ov_mod._voice_ref.update({"path": "/tmp/fake.wav", "text": "old"})

    class _FakeProc:
        def __init__(self):
            self.killed = False

        def poll(self):
            return None

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            return 0

    fake_proc = _FakeProc()
    ov_mod._sidecar_process = fake_proc

    spawn_calls = []
    monkeypatch.setattr(ov_mod.OmniVoiceProvider, "_spawn_sidecar", lambda self: spawn_calls.append(1))

    ov_mod.reset_voice_ref_and_respawn()

    assert ov_mod._voice_ref == {}
    assert fake_proc.killed is True
    assert len(spawn_calls) == 1
    ov_mod._sidecar_process = None  # reset module state
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/unit/test_omnivoice_provider.py -k "sidecar_tracks or reset_voice_ref_and_respawn" -v`
Expected: FAIL — `AttributeError: module '...omnivoice_provider' has no attribute '_sidecar_process'` / `'reset_voice_ref_and_respawn'`.

- [ ] **Step 3: Implement PID tracking + `reset_voice_ref_and_respawn`, then rewire every `settings.omnivoice_*` read**

Add module-level state (near `_voice_ref`):
```python
# Tracks the currently-running sidecar Popen handle so a settings change can kill
# it before spawning a replacement. None until the first _spawn_sidecar() call.
_sidecar_process: subprocess.Popen | None = None
```

Update `_spawn_sidecar` to store the handle and kill any previous one first:
```python
    def _spawn_sidecar(self) -> None:
        global _sidecar_process
        if _sidecar_process is not None and _sidecar_process.poll() is None:
            _sidecar_process.kill()
            try:
                _sidecar_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        omnivoice = system_config_store.get().omnivoice
        sidecar = Path(__file__).resolve().parent.parent / "omnivoice_sidecar.py"
        cmd = [
            omnivoice.omnivoice_python_path, str(sidecar),
            "--host", omnivoice.omnivoice_server_host,
            "--port", str(omnivoice.omnivoice_server_port),
            "--model", get_active_omnivoice_model(),
            "--dtype", omnivoice.omnivoice_dtype,
        ]
        if omnivoice.omnivoice_device:
            cmd += ["--device", omnivoice.omnivoice_device]
        logger.info("Starting OmniVoice sidecar server on port %s", omnivoice.omnivoice_server_port)
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
        log_fh = open(  # noqa: SIM115 - kept open for the child's lifetime
            Path(settings.artifacts_dir).resolve() / "_omnivoice_sidecar.log", "ab"
        )
        _sidecar_process = subprocess.Popen(  # noqa: S603 - local model server
            cmd, cwd=omnivoice.omnivoice_path, env=env,
            stdout=log_fh, stderr=log_fh, start_new_session=True,
        )
```

Add the module-level reset function (after `set_active_omnivoice_model`):
```python
def reset_voice_ref_and_respawn() -> None:
    """Clear the pinned-voice cache and kill+respawn the sidecar so an admin edit
    to model_id/dtype/device/host/port/instruct/temperature takes effect without a
    process restart. The sidecar has no reload endpoint (see omnivoice_sidecar.py) —
    a new process with the new CLI args is the only way to pick up the change."""
    _voice_ref.clear()
    provider = OmniVoiceProvider()
    provider._spawn_sidecar()
```

Now rewire every other `settings.omnivoice_*` read in the file to `system_config_store.get().omnivoice.*`:

```python
    def available(self) -> bool:
        return os.path.isfile(settings.omnivoice_python_path)
```
→
```python
    def available(self) -> bool:
        return os.path.isfile(system_config_store.get().omnivoice.omnivoice_python_path)
```

```python
    async def _synth(
        self, text: str, *, instruct=None, ref_audio=None, ref_text=None, speed=None
    ) -> bytes:
        if settings.omnivoice_use_server:
            return await self._server_synth(text, instruct, ref_audio, ref_text, speed)
        return await self._cli_synth(text, instruct, ref_audio, ref_text, speed)
```
→
```python
    async def _synth(
        self, text: str, *, instruct=None, ref_audio=None, ref_text=None, speed=None
    ) -> bytes:
        if system_config_store.get().omnivoice.omnivoice_use_server:
            return await self._server_synth(text, instruct, ref_audio, ref_text, speed)
        return await self._cli_synth(text, instruct, ref_audio, ref_text, speed)
```

```python
    async def _render_wav(self, payload: TTSRequest) -> bytes:
        logger.info("DEBUG_HANG _render_wav: text_len=%d", len(payload.text or ""))
        instruct, ref_audio, ref_text = payload.instruct, payload.ref_audio_path, payload.ref_text
        if settings.omnivoice_pin_voice and not ref_audio and not instruct:
            ref = await self._ensure_voice_ref()
            ref_audio, ref_text = ref["path"], ref["text"]
        elif not ref_audio and not instruct:
            instruct = settings.omnivoice_default_instruct
        return await self._synth(
            payload.text, instruct=instruct, ref_audio=ref_audio, ref_text=ref_text, speed=payload.speed
        )
```
→
```python
    async def _render_wav(self, payload: TTSRequest) -> bytes:
        logger.info("DEBUG_HANG _render_wav: text_len=%d", len(payload.text or ""))
        omnivoice = system_config_store.get().omnivoice
        instruct, ref_audio, ref_text = payload.instruct, payload.ref_audio_path, payload.ref_text
        if omnivoice.omnivoice_pin_voice and not ref_audio and not instruct:
            ref = await self._ensure_voice_ref()
            ref_audio, ref_text = ref["path"], ref["text"]
        elif not ref_audio and not instruct:
            instruct = omnivoice.omnivoice_default_instruct
        return await self._synth(
            payload.text, instruct=instruct, ref_audio=ref_audio, ref_text=ref_text, speed=payload.speed
        )
```

```python
    async def _ensure_voice_ref(self) -> dict[str, str]:
        if _voice_ref.get("path") and os.path.isfile(_voice_ref["path"]):
            return _voice_ref
        async with _voice_ref_lock:
            if _voice_ref.get("path") and os.path.isfile(_voice_ref["path"]):
                return _voice_ref
            ref_dir = Path(settings.artifacts_dir).resolve()
            ref_dir.mkdir(parents=True, exist_ok=True)
            ref_path = str(ref_dir / "_omnivoice_voice_ref.wav")
            wav = await self._synth(
                settings.omnivoice_ref_text, instruct=settings.omnivoice_default_instruct
            )
            Path(ref_path).write_bytes(wav)
            _voice_ref.update({"path": ref_path, "text": settings.omnivoice_ref_text})
            return _voice_ref
```
→
```python
    async def _ensure_voice_ref(self) -> dict[str, str]:
        if _voice_ref.get("path") and os.path.isfile(_voice_ref["path"]):
            return _voice_ref
        async with _voice_ref_lock:
            if _voice_ref.get("path") and os.path.isfile(_voice_ref["path"]):
                return _voice_ref
            omnivoice = system_config_store.get().omnivoice
            ref_dir = Path(settings.artifacts_dir).resolve()
            ref_dir.mkdir(parents=True, exist_ok=True)
            ref_path = str(ref_dir / "_omnivoice_voice_ref.wav")
            wav = await self._synth(
                omnivoice.omnivoice_ref_text, instruct=omnivoice.omnivoice_default_instruct
            )
            Path(ref_path).write_bytes(wav)
            _voice_ref.update({"path": ref_path, "text": omnivoice.omnivoice_ref_text})
            return _voice_ref
```
(`settings.artifacts_dir` stays as `settings.*` — it's a bootstrap-only path field per the spec, not migrated.)

```python
    def _server_base(self) -> str:
        return f"http://{settings.omnivoice_server_host}:{settings.omnivoice_server_port}"
```
→
```python
    def _server_base(self) -> str:
        omnivoice = system_config_store.get().omnivoice
        return f"http://{omnivoice.omnivoice_server_host}:{omnivoice.omnivoice_server_port}"
```

```python
    def warm(self) -> None:
        if not settings.omnivoice_use_server:
            return
        try:
            if httpx.get(f"{self._server_base()}/health", timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        self._spawn_sidecar()
```
→
```python
    def warm(self) -> None:
        if not system_config_store.get().omnivoice.omnivoice_use_server:
            return
        try:
            if httpx.get(f"{self._server_base()}/health", timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        self._spawn_sidecar()
```

```python
    async def _ensure_server(self) -> None:
        if await self._server_up():
            return
        self._spawn_sidecar()
        deadline = settings.omnivoice_server_startup_seconds
        waited = 0.0
        while waited < deadline:
            await asyncio.sleep(1.0)
            waited += 1.0
            if await self._server_up():
                return
        raise RuntimeError("OmniVoice server did not become ready in time")
```
→
```python
    async def _ensure_server(self) -> None:
        if await self._server_up():
            return
        self._spawn_sidecar()
        deadline = system_config_store.get().omnivoice.omnivoice_server_startup_seconds
        waited = 0.0
        while waited < deadline:
            await asyncio.sleep(1.0)
            waited += 1.0
            if await self._server_up():
                return
        raise RuntimeError("OmniVoice server did not become ready in time")
```

```python
    async def _server_synth(self, text, instruct, ref_audio, ref_text, speed) -> bytes:
        await self._ensure_server()
        body = {
            "text": text,
            "language": None,
            "instruct": instruct,
            "ref_audio": ref_audio,
            "ref_text": ref_text,
            "speed": speed,
            "class_temperature": settings.omnivoice_class_temperature,
        }
        logger.info("DEBUG_HANG _server_synth: posting /synth, text_len=%d", len(text))
        async with httpx.AsyncClient(timeout=settings.omnivoice_timeout_seconds) as client:
```
→
```python
    async def _server_synth(self, text, instruct, ref_audio, ref_text, speed) -> bytes:
        await self._ensure_server()
        omnivoice = system_config_store.get().omnivoice
        body = {
            "text": text,
            "language": None,
            "instruct": instruct,
            "ref_audio": ref_audio,
            "ref_text": ref_text,
            "speed": speed,
            "class_temperature": omnivoice.omnivoice_class_temperature,
        }
        logger.info("DEBUG_HANG _server_synth: posting /synth, text_len=%d", len(text))
        async with httpx.AsyncClient(timeout=omnivoice.omnivoice_timeout_seconds) as client:
```

```python
    def _build_cmd(self, text, instruct, ref_audio, ref_text, speed, output_path) -> list[str]:
        cmd = [
            settings.omnivoice_python_path, "-m", "omnivoice.cli.infer",
            "--model", get_active_omnivoice_model(), "--text", text, "--output", output_path,
            "--class_temperature", str(settings.omnivoice_class_temperature),
        ]
        if settings.omnivoice_device:
            cmd += ["--device", settings.omnivoice_device]
```
→
```python
    def _build_cmd(self, text, instruct, ref_audio, ref_text, speed, output_path) -> list[str]:
        omnivoice = system_config_store.get().omnivoice
        cmd = [
            omnivoice.omnivoice_python_path, "-m", "omnivoice.cli.infer",
            "--model", get_active_omnivoice_model(), "--text", text, "--output", output_path,
            "--class_temperature", str(omnivoice.omnivoice_class_temperature),
        ]
        if omnivoice.omnivoice_device:
            cmd += ["--device", omnivoice.omnivoice_device]
```

```python
    async def _cli_synth(self, text, instruct, ref_audio, ref_text, speed) -> bytes:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            output_path = tmp.name
        try:
            cmd = self._build_cmd(text, instruct, ref_audio, ref_text, speed, output_path)
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=settings.omnivoice_path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=settings.omnivoice_timeout_seconds
                )
```
→
```python
    async def _cli_synth(self, text, instruct, ref_audio, ref_text, speed) -> bytes:
        omnivoice = system_config_store.get().omnivoice
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            output_path = tmp.name
        try:
            cmd = self._build_cmd(text, instruct, ref_audio, ref_text, speed, output_path)
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=omnivoice.omnivoice_path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=omnivoice.omnivoice_timeout_seconds
                )
```

Also update `get_active_omnivoice_model`:
```python
def get_active_omnivoice_model() -> str:
    return _active_model or settings.omnivoice_model_id
```
→
```python
def get_active_omnivoice_model() -> str:
    return _active_model or system_config_store.get().omnivoice.omnivoice_model_id
```

Add `from app.services.system_config import system_config_store` to this file's imports. `settings` stays imported (still used for `settings.artifacts_dir`).

- [ ] **Step 4: Run tests, verify they pass**

Run: `pytest tests/unit/test_omnivoice_provider.py -v`
Expected: PASS, including the 3 pre-existing tests (they mock at `_ensure_server`/`httpx.AsyncClient`/`_synth` level, above where these edits land, so they're unaffected — confirm this by reading the diff against the mocking boundaries noted in the research).

- [ ] **Step 5: `recommend/capabilities.py` and `system.py` — `omnivoice_path`**

```python
    try:
        modules["omnivoice"] = os.path.isdir(settings.omnivoice_path)
    except Exception:  # noqa: BLE001
        modules["omnivoice"] = False
```
→
```python
    try:
        modules["omnivoice"] = os.path.isdir(system_config_store.get().omnivoice.omnivoice_path)
    except Exception:  # noqa: BLE001
        modules["omnivoice"] = False
```

`system.py`:
```python
        "tts": {
            "omnivoice_path": settings.omnivoice_path,
            "omnivoice_present": os.path.isdir(settings.omnivoice_path),
        },
```
→
```python
        "tts": {
            "omnivoice_path": omnivoice.omnivoice_path,
            "omnivoice_present": os.path.isdir(omnivoice.omnivoice_path),
        },
```
Add `omnivoice = system_config_store.get().omnivoice` to the same block of locals as `preprocessing`/`stt_local` from Tasks 4/5's edits to this function.

- [ ] **Step 6: Delete migrated fields from `Settings`**

Delete: `omnivoice_path`, `omnivoice_model_id`, `omnivoice_device`, `omnivoice_dtype`, `omnivoice_python`, `omnivoice_timeout_seconds`, `omnivoice_use_server`, `omnivoice_server_host`, `omnivoice_server_port`, `omnivoice_server_startup_seconds`, `omnivoice_default_instruct`, `omnivoice_class_temperature`, `omnivoice_pin_voice`, `omnivoice_ref_text`, `default_tts_engine_voice`, and the `omnivoice_python_path` property (moved onto `OmnivoiceConfig` in Task 1).

- [ ] **Step 7: Wire the final cache-invalidation hook**

```python
    new_config = system_config_store.set(merged)
    if current.stt_local.qwen3_asr_device != new_config.stt_local.qwen3_asr_device:
        from app.services.stt.providers.qwen3_asr_provider import clear_model_cache

        clear_model_cache()
    if (
        current.preprocessing.pyannote_vad_model != new_config.preprocessing.pyannote_vad_model
        or current.preprocessing.pyannote_auth_token != new_config.preprocessing.pyannote_auth_token
    ):
        from app.services.vad import clear_pyannote_cache

        clear_pyannote_cache()
    if current.remote_stt != new_config.remote_stt:
        from app.services.stt.service import stt_service

        stt_service.reinit_remote_providers(new_config.remote_stt)
    if current.omnivoice != new_config.omnivoice:
        from app.services.tts.providers.omnivoice_provider import reset_voice_ref_and_respawn

        reset_voice_ref_and_respawn()
    return {"success": True, "data": _mask_system_config(new_config)}
```
This completes the cache-invalidation dispatcher started in Task 1.

- [ ] **Step 8: Write a test for the wired hook, run it, verify fail then pass**

Add to `tests/unit/test_system_config_routes.py`:
```python
def test_changing_omnivoice_model_id_clears_voice_ref_and_respawns(client, monkeypatch):
    from app.services.tts.providers import omnivoice_provider as ov_mod

    ov_mod._voice_ref.update({"path": "/tmp/old.wav", "text": "old"})
    spawn_calls = []
    monkeypatch.setattr(ov_mod.OmniVoiceProvider, "_spawn_sidecar", lambda self: spawn_calls.append(1))

    full = client.get("/v1/system/config").json()["data"]
    full["omnivoice"]["omnivoice_model_id"] = "k2-fsa/OmniVoice-v2"
    client.put("/v1/system/config", json=full)

    assert ov_mod._voice_ref == {}
    assert len(spawn_calls) == 1
```
Run: `pytest tests/unit/test_system_config_routes.py -k omnivoice -v` — verify FAIL before Step 7, PASS after.

- [ ] **Step 9: Update existing tests, run the full suite, commit**

Grep `tests/` for the 16 fields/property deleted in Step 6 and update.

```bash
git add apps/api_gateway/app/services/tts/providers/omnivoice_provider.py \
  apps/api_gateway/app/services/recommend/capabilities.py apps/api_gateway/app/api/routes/system.py \
  apps/api_gateway/app/core/settings.py tests/
git commit -m "feat(system-config): migrate OmniVoice settings off .env, track sidecar PID, respawn on save"
```

---

### Task 8: Cleanup — `.env.example`, dangling fields, docs

**Files:**
- Modify: `.env.example`
- Modify: `apps/api_gateway/app/core/settings.py` (final sweep)
- Modify: `README.md` and any other doc referencing the migrated env vars (grep first)

**Interfaces:** none — this task only deletes/documents, no new code paths.

- [ ] **Step 1: Trim `.env.example`**

Delete every block for a field migrated in Tasks 2–7 (all of: `DEFAULT_STT_ENGINE`, `DEFAULT_TTS_ENGINE`, `OMNIVOICE_*`, `DEFAULT_TTS_ENGINE_VOICE`, `STT_MODEL_DIR`, `VOSK_MODEL_PATH`, `STT_STREAM_SAMPLE_RATE`, `WHISPER_LOCAL_*`, `WHISPER_BEAM_SIZE`, `WHISPER_CONDITION_ON_PREVIOUS_TEXT`, `WHISPER_INITIAL_PROMPT`, `WHISPER_MLX_MODEL_PATH`, `EXTRA_WARMUP_*_ENGINES`, `STT_VAD_*`, `STT_NOISE_REDUCE_*`, `PYANNOTE_*`, `CONVERSATION_SILENCE_MS` and every other `CONVERSATION_*` tuning var, `CONVERSATION_LLM_*`, `WHISPER_SERVICE_*`, `EVENTLAB_*`, `REMOTE_STT_TIMEOUT_SECONDS`). Replace with:
```
# STT/TTS engine choice, Whisper/OmniVoice/Qwen3 model settings, conversation LLM
# endpoint, remote STT provider endpoints/keys, conversation tuning, and VAD/noise
# preprocessing all moved to the admin UI (System tab > System settings). They are
# no longer read from .env — see docs/superpowers/specs/2026-07-13-env-to-admin-system-settings-design.md
```
Keep every bootstrap-only var untouched (`APP_NAME`, `APP_ENV`, `APP_HOST`, `APP_PORT`, `LOG_LEVEL`, `CORS_ALLOW_ORIGINS`, `ADMIN_PASSWORD`, `SESSION_SECRET`, `DEVICE_AUTH_TOKEN`, `ADMIN_BOOTSTRAP_*`, `ARTIFACTS_DIR`, and anything for MCP/device-MCP/Livehost/paths/`DATABASE_URL`/`ALLOW_RUNTIME_INSTALL` not covered by this migration).

- [ ] **Step 2: Confirm no dangling fields in `Settings`**

Run: `grep -n "conversation_llm\|omnivoice_\|whisper_local\|whisper_mlx\|qwen3_asr\|stt_vad\|stt_noise_reduce\|pyannote\|whisper_service\|eventlab\|default_stt_engine\|default_tts_engine\|extra_warmup\|warmup_on_startup\|warmup_startup_timeout\|conversation_silence_ms\|conversation_min_\|conversation_adaptive\|conversation_rms\|conversation_preroll\|conversation_max_utterance\|conversation_goodbye\|conversation_stt_engine\|conversation_fast_stt\|conversation_streaming\|conversation_tts_\|conversation_opus\|conversation_language\|conversation_system_prompt\|stt_model_dir\|vosk_model\|stt_stream_sample_rate\|whisper_beam_size\|whisper_condition\|whisper_initial_prompt\|stt_glossary\|stt_profile\|stt_enhance\|stt_segment\|ollama_bin" apps/api_gateway/app/core/settings.py`
Expected: no output (every one of these fields was deleted across Tasks 2–7). If any remain, delete them now.

- [ ] **Step 3: Update docs**

Run: `grep -rln "OMNIVOICE_\|WHISPER_LOCAL_\|CONVERSATION_LLM_\|DEFAULT_STT_ENGINE\|DEFAULT_TTS_ENGINE" README.md docs/ 2>/dev/null` and update any hit describing these as `.env` variables to instead point at the admin System settings panel.

- [ ] **Step 4: Run the full test suite one final time**

Run: `pytest`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add .env.example apps/api_gateway/app/core/settings.py README.md docs/
git commit -m "docs: trim .env.example and docs for settings migrated to admin System settings"
```

---

## Self-Review Notes (for whoever executes this plan)

- **Dead fields, no migration test possible:** `conversation_streaming_stt`, `conversation_streaming_chunk_ms` (Task 3) — field exists in `ConversationTuningConfig` with matching default, deleted from `Settings`, no call site to rewire.
- **No external call site, only an internal property:** `extra_warmup_stt_engines`, `extra_warmup_tts_engines` (Task 2) — migration is the `warmup_stt_engines()`/`warmup_tts_engines()` function bodies in `system_config.py`, not an external file.
- **Runtime-override precedence preserved, not changed:** `_active_model`/`_active_path` globals in `whisper_provider.py`, `vosk_provider.py`, `qwen3_asr_provider.py`, `omnivoice_provider.py` still win over the config store — only their fallback target moved from `settings.X` to `system_config_store.get().group.X`.
- **`qwen3_asr` device-cache staleness is a pre-existing, intentionally-preserved quirk**, not introduced by this migration — the cache key never included device before or after; Task 4's `clear_model_cache()` hook is what makes an admin's device change actually take effect (previously, only a process restart would).
- **`ModelManager`/`STTService`/`TTSService`'s module-level singletons still bake in some config at import time** (e.g. `ModelManager._base` in Task 4 Step 9) — this is unchanged, pre-existing behavior (same as when they read `settings.X`, which was equally frozen at import time for `Settings`, a `BaseSettings` singleton). Only the 4 fields explicitly named in the spec's Cache invalidation table get a live-reload hook; nothing else was promised to be live-editable without restart.
