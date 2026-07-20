# Model Registry as Single Source of Truth for Model Selection

**Date:** 2026-07-20
**Status:** Approved — ready for implementation plan

## Problem

Three distinct systems share the word "model" and their responsibilities are tangled:

1. **Model Registry** (`app/services/model_registry/`, DB table `model_registry_entries`, `/v1/model_registry`) — admin-curated allow-list + credentials + config, shape `(kind, engine, model_id, label, enabled, stage, api_key, base_url, config)`.
2. **Models admin page** (`/v1/models`, `model-manager.js`) — artifact lifecycle (download/install/select/delete of weights), driven by hardcoded per-engine catalogs + filesystem/HF-cache/Ollama state.
3. **`app/services/stt/model_registry.py`** (`STT_MODEL_REGISTRIES`) — a hardcoded engine→manager map for listing local variants, confusingly named "model registry" but belonging to system #2.

The profile STT MODEL dropdown (and most other model selects) is built from systems #2/#3 (`GET /v1/stt/engines` + `GET /v1/stt/models` = hardcoded catalogs + install state). The Model Registry participates only invisibly: as an availability signal inside `list_engines` and as a save-time gate. Consequences:

- An admin curating Registry entries does **not** shape what users see in the dropdown.
- A registry-disabled model still appears in the dropdown (errors only on save).
- A registry entry for an uncatalogued model never appears.
- Gate semantics ("no entry = unrestricted") conflict with `llm-options` ("only registry entries are options").
- TTS gating passes `(engine, engine)` as `(engine, model_id)` — a shim.

## Goal

Two clean concepts, no overlap:

- **Models (Artifact lifecycle)** — the "warehouse". Download/install/select/delete weights. Nobody *chooses a model to use* here.
- **Model Registry (Source of truth)** — the single catalog every profile and service reads to choose a model, for STT + TTS + LLM. Dropdowns show only `enabled` entries valid for the user's `stage`.

Bridge: installing a local model auto-creates/enables its Registry entry; deleting it disables (never deletes) the entry.

## Decisions (locked)

- **Auto-sync on install** — install via Models page auto-syncs to Registry; admin need not act twice.
- **Registry = single catalog** — all model dropdowns read only `enabled` + stage-valid Registry entries.
- **Scope** — STT + TTS + LLM, both UIs (playground static + lugo-web-client).
- **TTS granularity** — entries carry a real `model_id` (e.g. `kokoro — v0.19`, `qwen3_tts — 0.6B`), matching STT/LLM shape. Voice selection stays in the TTS profile.
- **Drop custom/BYO** — remove `__custom__` free-form LLM option; gate becomes catalog-mode (must have enabled entry).
- **Delete = disable, not remove** — keeps admin-entered api_key/config; Registry has no DELETE.
- **Unified options endpoint** — `/v1/model_registry/options?kind=` for all three kinds; `/v1/profiles/llm-options` removed entirely.

## Architecture

### Concept A — Models (Artifact lifecycle)

- Trang admin `MODELS` (`section-models`), `model-manager.js`, `GET /v1/models` + per-engine `download/select/delete` in `routes/system.py`.
- Data source: hardcoded catalogs (`WHISPER_SIZES`, `VOSK_SUGGESTIONS`, `OMNIVOICE_MODELS`, `VIENEU_MODES`, `LLM_SUGGESTIONS`, `QWEN3_ASR_MODELS`) + filesystem/HF-cache/Ollama state.
- Unchanged in responsibility. Gains only auto-sync hooks (see below).

### Concept B — Model Registry (Source of truth for selection)

- DB table `model_registry_entries`, `app/services/model_registry/`, `/v1/model_registry`.
- The single catalog for choosing STT/TTS/LLM models. Dropdowns read it (filtered) via a new unified `options` endpoint.

### Rename to kill the name collision

`app/services/stt/model_registry.py` (`STT_MODEL_REGISTRIES`) belongs to Concept A (artifact catalog). Rename:

- File: `app/services/stt/model_registry.py` → `app/services/stt/model_catalog.py`
- Symbol: `STT_MODEL_REGISTRIES` → `STT_MODEL_CATALOGS`
- Update all imports (`routes/stt.py`, any providers).

This is a mechanical, behavior-preserving step done first and verified before the rest.

## Backend design

### Unified options endpoint

`GET /v1/model_registry/options?kind=stt|tts|llm`

- Returns only entries with `enabled=true` AND (`stage="stable"` OR user has `can_use_testing`).
- Response shape (per entry): `{ engine, model_id, label }` — no api_key, no config, no base_url.
- Replaces the two-call dropdown data path (`/v1/stt/engines` + `/v1/stt/models`) and the LLM-specific `/v1/profiles/llm-options` (removed).
- Generalizes the existing `llm-options` filtering logic in `routes/profiles.py:102-113`.

### Gate becomes catalog-mode

`check_model_allowed(kind, engine, model_id, user)` (`services/model_registry/gate.py`):

- **Old:** missing entry = unrestricted (BYO allowed).
- **New:** require an entry that is `enabled` and stage-valid for the user; otherwise reject.
- Applies to stt/tts/llm uniformly.
- Remove the `__custom__` / BYO code path.

### Save paths

- Profile STT/LLM: store `(engine, model_id)`, validate via new gate.
- TTS profile: store real `(engine, model_id)` instead of the `(engine, engine)` shim (`routes/tts_profiles.py:50,80`). Gate on the real pair.

### Internal endpoints retained

- `GET /v1/stt/engines`, `GET /v1/stt/models`, `GET /v1/tts/engines`: kept for runtime resolution and availability checks, but **no longer the source for any dropdown**.

## Auto-sync: Models → Registry

### Hooks in `routes/system.py` action endpoints

- On successful `POST /v1/models/{engine}/download`: `ensure_registry_entry(kind, engine, model_id, label)`:
  - No entry → create with `enabled=true`, `stage="stable"`.
  - Existing entry → ensure `enabled=true`; **do not overwrite** admin-edited config/api_key/base_url/stage.
- On `POST /v1/models/{engine}/delete`: `disable_registry_entry(kind, engine, model_id)` → set `enabled=false`, keep the row.

### Engine → (kind, catalog) map

A single map so auto-sync knows what entry to create per engine, sourced from the artifact catalogs (post-rename): whisper→`WHISPER_SIZES`, vosk→`VOSK_SUGGESTIONS`, qwen3_asr→`QWEN3_ASR_MODELS`, omnivoice→`OMNIVOICE_MODELS`, vieneu→`VIENEU_MODES`, llm(ollama)→installed tags.

### Seed migration (boot, idempotent)

Runs after existing seeds in `main.py`:

1. Scan installed local models (whisper dirs, vosk dirs, HF cache, ollama tags) → create `enabled` entries if absent.
2. Keep existing remote/BYO entries (openrouter, openai_stt) as-is.
3. For any model currently referenced by a profile or global/system config but lacking an entry → create an `enabled` entry so existing configs don't break.

### Remote engines

openrouter STT, openai_stt, edge_tts, qwen3_asr_or, etc. do **not** go through the Models page. Admin adds them directly in the Registry (existing Add Entry + test-on-create). Auto-sync only handles local models.

## Frontend design

### Playground (`app/static/js/`)

- `profiles.js`: `renderProfileSttModelSelect()` reads `options?kind=stt`; LLM select reads `options?kind=llm` and drops `__custom__`; TTS select uses `(engine, model_id)`.
- `stt-engines.js` / `tts-engines.js`: batch/stream/V2T/conversation/livehost selects read `options`.
- `system-config.js`: global STT/TTS defaults and default LLM read from `options` (unified; remove hand-filtered `/v1/model_registry` reads).

### lugo-web-client (submodule)

- `api/stt.ts`, `api/profiles.ts`: `listSttModelOptions()` and LLM options call `options?kind=`.
- `ProfileEditor.tsx`: STT/TTS/LLM selects read from options; LLM drops free-text datalist.

## Testing (TDD)

Backend:
- `options` endpoint: filters enabled/stage/permission; correct shape; per-kind.
- Gate (catalog-mode): reject on no-entry / disabled / testing-without-permission; accept on enabled+stable.
- Auto-sync: install→create/enable; install existing→enable without clobbering config; delete→disable, row retained.
- Seed migration: idempotent; picks up installed models + config-referenced models; leaves remote entries.

Frontend:
- Dropdown renders from options.
- Profile save round-trips `(engine, model_id)` for STT/TTS/LLM.

Scope: run only changed-repo tests (api_gateway + lugo-web-client); full suite is the pre-commit gate.

## Rollout order

Each step: tests green + local endpoint check before the next.

1. Rename `stt/model_registry.py` → `model_catalog.py` (mechanical).
2. `options` endpoint + catalog-mode gate + remove `llm-options`.
3. Auto-sync install/delete hooks + seed migration.
4. Frontend playground.
5. Frontend lugo-web-client.

## Out of scope

- Adding a DELETE to the Registry.
- Changing artifact download mechanics.
- Recommender catalog rework.
- ESP32/device-side changes.
