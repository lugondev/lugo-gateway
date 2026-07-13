# Move runtime settings from .env to Admin System Settings — design

**Date:** 2026-07-13
**Status:** Approved (design)

## Problem & goal

`apps/api_gateway/app/core/settings.py` (`Settings`, pydantic-settings, `.env`-backed) currently mixes two kinds of config:

1. **App bootstrap config** — truly static values needed before the DB is reachable, or that never change per-deployment intent: `admin_password`, `session_secret`, `device_auth_token`, `admin_bootstrap_*`, `database_url`, `app_host/app_port/log_level/cors_allow_origins`, store file paths, MCP/device-MCP settings, Livehost tuning.
2. **Runtime/model tuning config** — engine choices, model paths, remote endpoints/keys, and turn-taking knobs that an admin plausibly wants to change *without editing a file and restarting the process*: STT/TTS engine defaults, Whisper/OmniVoice/Qwen3 model settings, conversation LLM endpoint, remote STT provider endpoints/keys, conversation tuning, VAD/noise-reduce preprocessing.

Goal: move all of group 2 into the existing SQLite-backed `system_config_store` (`apps/api_gateway/app/services/system_config.py`), editable via the existing `/v1/system/config` admin UI panel, and remove those fields from `Settings`/`.env` entirely. `.env` keeps only group 1.

## Key decisions (approved)

1. **Full runtime-tuning scope** — migrate all 7 groups below, not just engine/model selection.
2. **No `.env` → DB import/fallback.** Unlike the JSON→SQLite store migration (see `2026-07-08-config-stores-to-sqlite-design.md`), there is no legacy source to import from here — `.env` values for the migrated fields are simply deleted. `SystemConfig` fields carry hard-coded Pydantic defaults matching today's `settings.py` code defaults (not the sometimes-stale `.env.example`), so a fresh deploy still boots sanely; an admin edits values that need to differ (e.g. `omnivoice_path`, LLM endpoint) via the UI after first boot.
3. **Nested-group data model, single row.** `SystemConfig` stays one `config_system` row (`id=1`), but gains 7 nested sub-models instead of ~40 flat fields. No schema/table migration needed — the row is a JSON blob (`data` column), so new nested fields just take their Pydantic default when an old row is read.
4. **Secrets are masked**, reusing the existing `openrouter_api_key` pattern: GET returns `"***"` for any non-empty secret field; PUT only overwrites a secret field if the incoming value is non-empty and not `"***"`.
5. **Cache invalidation on save**, not restart-required. Several existing singletons/caches read settings only once (at construction or first use). `PUT /v1/system/config` diffs old vs. new config and calls a targeted invalidation hook per affected group, so admin edits take effect without a process restart.
6. **One admin UI panel**, extended (not a new page/tab) — same `/v1/system/config` GET/PUT round-trip, same static panel in `static/index.html`, grouped into collapsible `<fieldset>`/`<details>` sections.
7. **Phased rollout by settings group** (below) to keep each change reviewable and to isolate risk — especially around OmniVoice's sidecar-process lifecycle, the highest-risk group.

## Data model

`apps/api_gateway/app/services/system_config.py` — `SystemConfig` keeps its 2 existing fields and adds 7 nested groups:

Field lists below are copied verbatim from the current `apps/api_gateway/app/core/settings.py` (read directly, not from `.env.example` — a few `.env.example` values have drifted from the code defaults, e.g. `OMNIVOICE_TIMEOUT_SECONDS=600` in the example vs. `45.0` in code; the code is authoritative).

```python
class EngineDefaults(BaseModel):
    default_stt_engine: str = "vosk"
    default_tts_engine: str = "omnivoice"
    extra_warmup_stt_engines: str = ""   # comma-separated, kept as str (matches settings.py)
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

class ConversationLlmConfig(BaseModel):
    conversation_llm_base_url: str = ""
    conversation_llm_api_key: str = ""    # secret, masked
    conversation_llm_model: str = "gpt-3.5-turbo"
    conversation_llm_timeout_seconds: float = 60.0
    ollama_bin: str = ""

class RemoteSttConfig(BaseModel):
    whisper_service_base_url: str = ""
    whisper_service_api_key: str = ""     # secret, masked
    whisper_service_model: str = "whisper-1"
    eventlab_base_url: str = ""
    eventlab_api_key: str = ""            # secret, masked
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
    stt_vad_backend: str = "energy"       # energy | silero | pyannote
    stt_noise_reduce_enabled: bool = False
    stt_noise_reduce_amount: float = 0.85
    pyannote_vad_model: str = "pyannote/segmentation-3.0"
    pyannote_auth_token: str = ""         # secret, masked

class SystemConfig(BaseModel):
    base_context: str = ""
    openrouter_api_key: str = ""          # secret, masked (existing)
    engines: EngineDefaults = EngineDefaults()
    stt_local: SttLocalConfig = SttLocalConfig()
    omnivoice: OmnivoiceConfig = OmnivoiceConfig()
    conversation_llm: ConversationLlmConfig = ConversationLlmConfig()
    remote_stt: RemoteSttConfig = RemoteSttConfig()
    conversation: ConversationTuningConfig = ConversationTuningConfig()
    preprocessing: PreprocessingConfig = PreprocessingConfig()
```

`Settings.warmup_stt_engines`/`warmup_tts_engines` (computed properties combining `conversation_stt_engine`/`conversation_tts_engine` + the `extra_warmup_*` lists) move to equivalent computed properties/helpers reading from `system_config_store.get()` instead of `self`.

## API

`GET/PUT /v1/system/config` (`apps/api_gateway/app/api/routes/system.py`) unchanged in shape, response/request body grows to the full nested `SystemConfig`.

Masking, generalized from the existing `openrouter_api_key` behavior to all 5 secret fields (`openrouter_api_key`, `conversation_llm_api_key`, `whisper_service_api_key`, `eventlab_api_key`, `pyannote_auth_token`):
- **GET**: any non-empty secret field is returned as `"***"`.
- **PUT**: a secret field is only overwritten if the incoming value is non-empty and not equal to `"***"`; otherwise the stored value is left untouched. This prevents a form re-submitting its own masked placeholder from wiping the real secret.

## Cache invalidation

Several existing singletons/caches read settings once at construction/first-use rather than per-call, so a live admin edit needs an explicit invalidation step. After `system_config_store` persists a `PUT`, diff old vs. new config per group and call the corresponding hook — only for groups that actually changed:

| Group changed | Hook | What it does |
|---|---|---|
| `remote_stt` | `stt_service.reinit_remote_providers(new.remote_stt)` | Rebuild `RemoteWhisperProvider`/`OpenRouterSttProvider` instances inside the `STTService` singleton with the new base_url/api_key/model/timeout |
| `preprocessing.pyannote_vad_model` / `preprocessing.pyannote_auth_token` | `vad.clear_pyannote_cache()` | Drop `_pyannote_cache["pipeline"]` so the next VAD call rebuilds it |
| `stt_local.qwen3_asr_device` | `qwen3_asr_provider.clear_model_cache()` | Drop the device-keyed model cache entry so the next call rebuilds for the new device |
| `omnivoice` (any field) | `omnivoice_provider.reset_voice_ref_and_respawn()` | Clear the process-wide `_voice_ref`, and kill/let-respawn the OmniVoice sidecar so it comes back up with new model_id/dtype/device/host/port |

Groups with no hook (already live-read per call, no caching): `engines`, `stt_local` fields other than `qwen3_asr_device`, `conversation_llm`, `conversation`, `preprocessing` fields other than the two pyannote ones. Changing `system_config_store` values for these takes effect on the very next call/request with zero extra code.

## Settings.py / .env changes

**Removed from `Settings`** (moved to `SystemConfig`, exactly the fields enumerated in the 7 nested groups above): `default_stt_engine`, `default_tts_engine`, `extra_warmup_stt_engines`, `extra_warmup_tts_engines`, `warmup_on_startup`, `warmup_startup_timeout_s`, `stt_model_dir`, `vosk_model_path`, `vosk_model_base_url`, `stt_stream_sample_rate`, `whisper_local_model`, `whisper_local_device`, `whisper_local_compute_type`, `whisper_vad_filter`, `whisper_beam_size`, `whisper_condition_on_previous_text`, `whisper_initial_prompt`, `stt_glossary_path`, `stt_profile`, `whisper_mlx_model_path`, `qwen3_asr_model`, `qwen3_asr_device`, `stt_enhance_timeout_seconds`, `stt_enhance_prompt`, `stt_segment_long_enabled`, `stt_segment_min_seconds`, `stt_segment_concurrency`, `omnivoice_path`, `omnivoice_model_id`, `omnivoice_device`, `omnivoice_dtype`, `omnivoice_python`, `omnivoice_timeout_seconds`, `omnivoice_use_server`, `omnivoice_server_host`, `omnivoice_server_port`, `omnivoice_server_startup_seconds`, `omnivoice_default_instruct`, `omnivoice_class_temperature`, `omnivoice_pin_voice`, `omnivoice_ref_text`, `default_tts_engine_voice`, `conversation_llm_base_url`, `conversation_llm_api_key`, `conversation_llm_model`, `conversation_llm_timeout_seconds`, `ollama_bin`, `whisper_service_base_url`, `whisper_service_api_key`, `whisper_service_model`, `eventlab_base_url`, `eventlab_api_key`, `eventlab_model`, `remote_stt_timeout_seconds`, `conversation_silence_ms`, `conversation_min_silence_ms`, `conversation_adaptive_full_ms`, `conversation_min_speech_ms`, `conversation_rms_threshold`, `conversation_preroll_ms`, `conversation_max_utterance_ms`, `conversation_goodbye_text`, `conversation_stt_engine`, `conversation_fast_stt_engine`, `conversation_fast_stt_max_ms`, `conversation_streaming_stt`, `conversation_streaming_chunk_ms`, `conversation_tts_engine`, `conversation_tts_lookahead`, `conversation_opus_pace`, `conversation_opus_prebuffer_frames`, `conversation_language`, `conversation_system_prompt`, `stt_vad_enabled`, `stt_vad_backend`, `stt_noise_reduce_enabled`, `stt_noise_reduce_amount`, `pyannote_vad_model`, `pyannote_auth_token`. Also removed: the `warmup_stt_engines`/`warmup_tts_engines` computed properties (reimplemented reading from `system_config_store`).

**Stays in `Settings`/`.env`**: `app_name`, `app_env`, `app_host`, `app_port`, `log_level`, `cors_allow_origins`, `admin_password`, `session_secret`, `device_auth_token`, `admin_bootstrap_username`, `admin_bootstrap_password`, `allow_runtime_install` (security-sensitive: gates pip-install at runtime, kept alongside the other access-control secrets rather than treated as model tuning), `artifacts_dir`, `profiles_path`, `tts_profiles_path`, `mcp_servers_path`, `system_config_path`, `database_url`, MCP tooling settings (`mcp_tool_cache_ttl_seconds`, `mcp_connection_timeout_seconds`, `mcp_tool_timeout_seconds`, `conversation_tools_enabled`, `conversation_tool_max_iters`), device-MCP settings (`device_mcp_enabled`, `device_mcp_request_timeout_s`, `device_mcp_discovery_timeout_s`), and all `livehost_*` (TikTok co-host) tuning — out of scope for this migration; not part of the model/STT/TTS/LLM/conversation/preprocessing groups the scope decision covered.

**`.env.example`** — delete every migrated block; replace with a single pointer comment: `# STT/TTS engine, Whisper/OmniVoice, conversation LLM, remote STT, conversation tuning, preprocessing → set via Admin UI > System Settings (no longer read from .env)`.

No import path from old `.env` values into the DB — this is an explicit, approved data-loss-on-migrate tradeoff (see decision 2). Admins must re-enter any non-default value after this ships.

## Frontend UI

Extend the existing System Config panel (`apps/api_gateway/app/static/js/base-context.js`, rendered inside `static/index.html`) — rename to `system-config.js` to reflect the widened scope. No new page/tab.

- Keep `base_context` (textarea) and `openrouter_api_key` (password input) at the top, unchanged.
- Add one `<details><fieldset>` block per group, matching the nested model: **Engine Defaults**, **STT (Local Models)**, **OmniVoice (TTS)**, **Conversation LLM**, **Remote STT Providers**, **Conversation Tuning**, **Preprocessing (VAD/Noise)**. "Engine Defaults" defaults to expanded; the rest default to collapsed.
- Input types follow field types (text/number/checkbox/password). The 5 secret fields use the same masked-placeholder behavior as the existing `openrouter_api_key` field: display `***`, only send a new value if the user actually edited it.
- Single form, single `PUT /v1/system/config` submit with the full nested payload — no per-group API calls.
- On successful save, show a confirmation noting that remote STT / OmniVoice / VAD changes apply automatically in the background (cache invalidation handles it — no restart needed).

## Testing

- Extend `tests/unit/test_system_config.py`, `test_system_config_store.py`, `test_system_config_routes.py`: new fields + their defaults; masking for all 5 secret fields (not just `openrouter_api_key`); PUT with `***` or empty string is a no-op for that field.
- Add unit tests per invalidation hook (`reinit_remote_providers`, `clear_pyannote_cache`, `clear_model_cache`, `reset_voice_ref_and_respawn`): fires when its own group's relevant field(s) change, does **not** fire when an unrelated group changes (e.g. editing `base_context` must not respawn the OmniVoice sidecar).
- After removing each group of fields from `Settings`, grep/compile-check for lingering `settings.<removed_field>` references across `app/` and `tests/` and update them to `system_config_store.get().<group>.<field>`.
- Full existing suite (≈265+ tests as of the last config migration) must pass before merging any phase — per this repo's existing test-before-push-deploy practice.

## Rollout order

Each numbered step is its own commit/PR, gated on the full suite passing before moving to the next:

1. Data model + store (no consumer changes yet) + API + UI scaffold. App behavior unchanged — nothing reads the new fields yet.
2. **Engine Defaults** — lowest risk, already live-read, no cache. Remove from `Settings`.
3. **Conversation LLM** + **Conversation Tuning** — live-read, no cache.
4. **STT Local / Whisper / Qwen3** — add the qwen3 device-cache invalidation hook.
5. **Preprocessing** — add the pyannote cache invalidation hook.
6. **Remote STT** — add `reinit_remote_providers`, rebuilding the `STTService` provider instances.
7. **OmniVoice** — highest risk (sidecar process lifecycle); add `reset_voice_ref_and_respawn`.
8. Cleanup: trim `.env.example`, confirm no dangling `Settings` fields, update any docs/README that describe `.env` variables for the migrated groups.

## Non-goals

- No change to `admin_password`/`session_secret`/`device_auth_token`/`database_url`/host/port/log-level or any other bootstrap-only setting — these stay in `.env`.
- No import/fallback path from old `.env` values into `SystemConfig` — explicitly rejected (decision 2).
- No new admin page/tab — extends the existing System Config panel only.
- No change to the `SqliteBackedStore`/`config_system` table schema — still a single JSON-blob row.
