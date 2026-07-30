# QwenCloud Admin UI — Design

**Date:** 2026-07-25
**Status:** Approved design → implementation
**Scope:** Complete the admin Model Registry UI so an operator can create and configure a `qwencloud` STT entry entirely through the UI (today it is API-only). Follow-up to the `qwencloud` engine (merged commit 1cffbc2).

## 1. Problem
The `qwencloud` STT engine exists and works, but no admin-UI path creates an entry:
- The Add-Entry **engine dropdown** lists only `mode === "local"` engines (`model-registry.js` `_loadEngineOptions`), so `qwencloud` (remote) never appears.
- The **provider-linked path** infers the engine via `_effectiveEngine()` (`model-registry.js:70-88`): any non-OpenRouter provider → `http_stt`, so picking the "Qwen Cloud (DashScope)" provider preset would wrongly create an `http_stt` entry.
- The **config fields** the engine reads (`realtime_model`, `language`, `turn_detection`, `semantic_punctuation`) have no `config_schema_for("stt","qwencloud")`, so the Edit → Config **Form** can't render them — only Raw JSON.

## 2. Locked decisions
1. **Both creation paths** (user choice):
   - **Provider-link:** picking the "Qwen Cloud (DashScope)" provider creates a `qwencloud` entry (fix `_effectiveEngine`).
   - **Direct dropdown:** `qwencloud` is selectable in the engine dropdown (no provider needed — operator supplies api_key; base_url defaults).
2. **Full config form with enum dropdowns** (user choice): `realtime_model` and `turn_detection` render as `<select>`; other fields as input/checkbox. Requires adding an optional `choices` to the config-schema field spec and `<select>` support in the form renderer.
3. Config is set in the **Edit → Config** panel post-create (the existing pattern for every engine), now with a friendly form instead of Raw JSON. A freshly created qwen3 entry works on defaults; a fun-asr entry needs one Edit to set `realtime_model = fun-asr-realtime` (the enum dropdown makes this obvious).

## 3. Changes

### Backend
- **`app/services/model_registry/config_schema.py`**: add a `qwencloud` STT schema returning fields, each `{key, type, default, choices?}`:
  - `realtime_model` (str, default `"qwen3-asr-flash-realtime"`, choices `["qwen3-asr-flash-realtime","fun-asr-realtime"]`)
  - `language` (str, default `""`)
  - `turn_detection` (str, default `"server_vad"`, choices `["server_vad","manual"]`)
  - `semantic_punctuation` (bool, default `false`)
  - `timeout_seconds` (float, default `60.0`)
  Add optional `choices` support to the field dict (a new key; existing fields simply omit it).
- **`app/api/routes/model_registry.py`** `_location`/`_requires_base_url`: make `qwencloud` a `"service"` engine that is api-key-only (fixed endpoint, no admin base_url required) — mirror the OpenRouter STT treatment. Add `qwencloud` to the set that yields `location=="service"`, and make `_requires_base_url` return `False` for it (extend the OpenRouter carve-out, e.g. a `_FIXED_ENDPOINT_STT_ENGINES` set = OpenRouter engines ∪ {`qwencloud`}). These fields are surfaced on the engines list the admin UI reads, so a blank base_url reads as "not needed" rather than "misconfigured". (`stt_service.list_engines` already reports `mode:"remote"` + `configured` from Task 2 of the engine work — no change there.)

### Frontend (`app/static/js/model-registry.js`, maybe `index.html`/`styles.css`)
- **`_effectiveEngine()`**: in the `providerId && kind==="stt"` branch, if the provider's `base_url` contains `"dashscope"` → return `"qwencloud"` (before the OpenRouter/http_stt fallback).
- **`_loadEngineOptions()`**: include `qwencloud` in the engine dropdown for `kind==="stt"` even though it's remote (filter `mode === "local" || engine === "qwencloud"`). When selected with no provider, the existing no-provider base_url + api_key inputs apply; **prefill** base_url with `https://dashscope-intl.aliyuncs.com` and keep api_key required.
- **`_renderConfigForm()` / `_configFromForm()`**: when a field has `choices`, render a `<select>` (options = choices, current value selected) and read `.value` back as the field's typed value.
- **model_id suggestions**: when the effective engine is `qwencloud`, seed the model_id combobox with `["qwen3-asr-flash","fun-asr"]` (free-type still allowed).

## 4. Out of scope
- Setting config at create-time (config stays a post-create Edit step, consistent with all engines).
- Filtering the provider's advertised model list (free-type + the two suggestions suffice).
- Any backend engine behavior (done in commit 1cffbc2).

## 5. Testing
- Backend unit: `config_schema_for("stt","qwencloud")` returns the 5 fields with correct types/defaults/choices (extend `tests/unit/test_model_registry_config_schema.py`).
- Backend unit: creating an entry with `engine="qwencloud"` + `config` via the route persists it (extend `tests/unit/test_model_registry_routes.py` / `test_model_registry_provider_link.py`); the config_schema route serves the qwencloud schema.
- Backend unit: `list_engines` qwencloud row carries `requires_base_url: false` (extend the existing qwencloud list_engines test).
- Frontend: no JS test harness for the vanilla admin UI — verify edits with the Read tool (per project convention); the backend tests cover the data contract the JS depends on (`choices`, schema, list_engines fields).

## 6. Files touched
- `apps/api_gateway/app/services/model_registry/config_schema.py`
- `apps/api_gateway/app/services/stt/service.py` (`list_engines` qwencloud row)
- `apps/api_gateway/app/static/js/model-registry.js`
- possibly `apps/api_gateway/app/static/index.html` / `styles.css` (only if `<select>` needs markup/style)
- tests: `tests/unit/test_model_registry_config_schema.py`, `tests/unit/test_model_registry_routes.py` (or `_provider_link.py`), qwencloud `list_engines` test
