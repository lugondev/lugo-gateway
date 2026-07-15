# STT/TTS config → Model Registry (design)

**Date:** 2026-07-15
**Status:** Approved by user, proceeding to implementation plan.

## Context

Conversation LLM was just migrated off `SystemConfig` onto Model Registry (`kind="llm"` entries) so an admin sets a provider's `base_url`/`api_key` once per model instead of duplicating it into every profile. Three more `SystemConfig` groups still hold engine config the same way the old LLM one did, and the user wants them migrated too:

- **Remote STT** (`RemoteSttConfig`): `whisper_service_*`, `eventlab_*` — real remote APIs needing `base_url`/`api_key`, same shape as the LLM case.
- **STT Local** (`SttLocalConfig`): `whisper_local_device`, `whisper_local_compute_type`, `qwen3_asr_device`, etc. — local inference tuning, no credentials.
- **OmniVoice** (`OmnivoiceConfig`): sidecar path/device/dtype/server host-port — local process config, no credentials.

### What already exists (discovered during design, changes scope)

`model_registry/seed.py::seed_known_models()` **already** creates bare `kind="stt"` entries (one row per known model size, e.g. `whisper_local`/`phowhisper-tiny`, `whisper_local`/`phowhisper-medium`, ...) and `kind="tts"` entries (one row per installed TTS engine, e.g. `omnivoice`/`omnivoice`) — but only for the existing "disabled model gate" feature (reject profile creation against a disabled model/stage). These rows carry no `base_url`/`api_key`/`config` yet.

`static/js/model-registry.js` already has a working kind picker, add-entry form, and api_key editing for non-llm kinds (comment: *"tts: no current engine reads it, stored for a future key-requiring one"* — this work is that future use). `base_url` editing is currently gated to `kind === "llm"` only in the table.

So the DB schema, store (`ModelRegistryStore`, generic on `kind`, has a free-form `config: dict` column already used elsewhere), and most of the admin table/add-form already work. The gap is: resolution code doesn't read from these entries yet, `base_url`/`config` aren't editable in the UI for stt/tts, and there's no migration seed pulling real values out of `SystemConfig`.

## Decisions (from brainstorming)

1. **Scope:** all three groups (Remote STT + STT Local + OmniVoice), not just Remote STT.
2. **STT Local shape:** one registry row **per engine**, keyed with `model_id=""` (a reserved sentinel meaning "engine-level settings", distinct from the per-size governance rows `seed_known_models()` already creates under the same `(kind="stt", engine)` pair). `config: dict` holds `device`/`compute_type`/`timeout`/etc. Per-size *selection* (tiny/medium/large) stays exactly as-is via `app.services.whisper_models.whisper_manager` — untouched.
3. **OmniVoice shape:** a single row, `kind="tts", engine="omnivoice", model_id=<omnivoice_model_id>`. `config: dict` holds `path`/`device`/`dtype`/`python`/`timeout_seconds`/`use_server`/`server_host`/`server_port`/`server_startup_seconds`/`default_instruct`/`class_temperature`/`pin_voice`/`ref_text`.
4. **Remote STT shape:** one row per (engine, model_id) exactly like LLM — `whisper_service`/`whisper-1`, `eventlab`/`whisper-1` — `base_url`/`api_key`/`config={"timeout_seconds": ...}`.
5. **Migration:** one-time seed on boot (mirrors `migrate_conversation_llm_to_registry()`): if no enabled entry exists yet for a given `(kind, engine, model_id-or-sentinel)`, create one from the current `SystemConfig` value (read via `system_config_store.get_raw_group(...)`, same pattern as the LLM migration, so it survives even after the field is removed from the schema). No-op once any matching entry exists (including "nothing configured either way").
6. **Backward compat:** none needed. Remove `SttLocalConfig`, `OmnivoiceConfig`, `RemoteSttConfig` and their fields from `SystemConfig` entirely once migrated, remove the 3 corresponding cards (`stt_local`, `omnivoice`, `remote_stt`) from `system-config.js`'s `GROUPS` and `index.html`.

## Components to change

- `app/services/system_config.py` — remove the 3 config classes + their fields off `SystemConfig`.
- `app/services/model_registry/seed.py` — add `migrate_remote_stt_to_registry()`, `migrate_stt_local_to_registry()`, `migrate_omnivoice_to_registry()` (or one combined function), called at boot alongside the existing LLM migration.
- `app/services/stt/service.py` — read remote STT provider config (`whisper_service`, `eventlab`) from `model_registry_store.find_enabled("stt", engine)` instead of `system_config_store.get().remote_stt`.
- `app/services/stt/providers/*` (whisper_provider.py, qwen3_asr_provider.py, vosk if present) — read `device`/`compute_type` from the registry entry's `config` (`model_registry_store.find("stt", engine, "")`) instead of `system_config_store.get().stt_local`.
- `app/services/tts/providers/omnivoice_provider.py` (+ `omnivoice_sidecar.py`) — read path/device/dtype/server settings from the registry entry's `config` instead of `system_config_store.get().omnivoice`.
- `app/api/routes/model_registry.py` — no schema change needed (payload already generic), but confirm PATCH accepts arbitrary `config` dict updates.
- `static/js/model-registry.js` — un-gate `base_url` editing for `kind === "stt"` too (not just llm); add a generic `config` key/value mini-editor for entries that have one (rendered as a JSON textarea is the pragmatic v1 — a bespoke per-field form for `device`/`compute_type`/etc. can follow later if this feels clunky in practice).
- `static/js/system-config.js`, `static/index.html` — remove the 3 groups/cards.
- `docs/api.md` — update to describe the new resolution source.

## Testing

TDD per repo convention. New/updated:
- `tests/unit/test_model_registry_store.py` — `config` dict round-trips through `create`/`set_fields`/`find`.
- New `tests/unit/test_stt_remote_registry_migration.py`-style tests for the 3 migration functions (idempotent, no-op once enabled entry exists, pulls from raw SystemConfig group).
- `tests/unit/test_stt_service_openrouter.py` / a new remote-stt-specific test file — remote STT provider construction now reads registry, not SystemConfig.
- Provider-level tests (whisper_provider, omnivoice_provider) for reading `device`/`compute_type`/etc. from the registry entry's `config`.
- Remove/update tests that currently assert on `SttLocalConfig`/`OmnivoiceConfig`/`RemoteSttConfig` fields directly (`test_system_config_store.py`, `test_system_config_routes.py` likely reference these).
- Manual UI check: add a remote STT entry with base_url/api_key, toggle it, confirm STT provider picks it up; edit an omnivoice entry's config and confirm sidecar reads new device/dtype.

## Error handling

Same pattern as the LLM path: if no enabled entry exists for an engine a profile/session actually requests, fail with a clear `AppError` (matching the existing "unsupported engine" style), not a silent default — avoids a confusing prod state where a provider silently runs with empty config.

## Out of scope

- Changing `whisper_manager`'s size-selection mechanism.
- Any change to the OpenRouter-backed STT engines (`qwen3_asr_or`, `whisper_or`) — already registry-driven.
- Building a bespoke per-engine config form beyond a JSON textarea for `config` (v1 is intentionally plain).
