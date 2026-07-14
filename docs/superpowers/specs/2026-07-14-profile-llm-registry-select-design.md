# Profile Configuration: select LLM from Model Registry

## Problem

The "Profile Configuration" panel's LLM section (`LLM BASE URL`, `LLM MODEL`, `LLM API KEY`) is pure free-text. It never reads from the admin-managed Model Registry (`model_registry_entries`, kind=`llm`), so users must hand-type a base URL/model/key even when the admin has already added that model to the registry (with its own key). The backend already has the matching machinery — `Profile.llm.engine`/`llm.model` fields, `check_model_allowed()` validation in `profiles.py`, and `resolve_llm_override_from_registry()` in `responder.py` — but it's all dead code today because the UI never sets `llm.engine`.

The STT/TTS profile dropdowns next to it already show the pattern to follow: fetch a list, render as `<select>` options, default to "(inherit global)".

## Design

### Backend: new filtered listing endpoint

`GET /v1/model_registry` lives behind `AuthGuardMiddleware`'s `_ADMIN_PREFIXES` (`apps/api_gateway/app/core/auth_guard.py:28`), which gates by path *prefix* — any new route nested under `/v1/model_registry` would inherit the same admin-only 403, which breaks the "regular users too" requirement. Rather than touch the security middleware, add the listing endpoint under `/v1/profiles` instead, which is already in `_USER_PREFIXES` (any logged-in user, `auth_guard.py:20`).

Add `GET /v1/profiles/llm-options` to `apps/api_gateway/app/api/routes/profiles.py` (placed before the `/{name}` route so it isn't swallowed by that path param). Resolves the acting user via the existing `_resolve_acting_user(request)` helper (`profiles.py:43-49`) and returns `model_registry_store.list_all()` entries filtered to:

- `kind == "llm"`
- `enabled` is true
- `stage != "testing"` OR the acting user has `can_use_testing = true`

Response strips `api_key`/`base_url` entirely (not just masked) — a non-admin picking a model has no reason to see even a masked admin key. Each item returns only `id`, `engine`, `model_id`, `label`.

No schema changes. `LlmConfig.engine` and `LlmConfig.model` already exist on `Profile`; they're just never populated by the UI today.

### Frontend: dropdown + Custom fallback

In `index.html`, replace the always-visible 3 free-text LLM inputs with:

1. `#pf-llm-select` — populated on panel open from `GET /v1/profiles/llm-options`, rendered as `label (engine/model_id)` options, plus a trailing `── Custom… ──` option.
2. `#pf-llm-custom-fields` — a wrapper div around the existing `#pf-llm-url`/`#pf-llm-model`/`#pf-llm-key` inputs, hidden unless "Custom" is selected.

In `profiles.js`:

- `loadLlmRegistryOptions()` / `renderProfileLlmSelect()`, mirroring `renderProfileTtsSelect()` (`profiles.js:51-63`).
- `openProfilePanel()`: if the profile's `llm.engine` is set and matches a usable entry, pre-select it and hide the custom fields. Otherwise select "Custom" and populate the free-text fields as today (preserves existing profiles unchanged).
- `saveProfile()`: if a registry entry is selected, set `llm.engine`/`llm.model` from it and clear `llm.base_url`/`llm.api_key` (empty string) so `resolve_llm_override_from_registry()` supplies them at call time. If "Custom" is selected, behave exactly as today (`llm.engine` stays empty, free-text fields sent as-is, including "leave blank to keep existing" for the API key).

### Error handling

- Registry fetch failure (network/500) on panel open: log/toast, fail open to "Custom" mode so editing isn't blocked.
- Race condition where a selected entry is disabled/testing-restricted between page load and save: already caught server-side by the existing `_validate_profile_models()` → `check_model_allowed()` call in `profiles.py`, surfaced through the existing save-error toast path in `saveProfile()`. No new error handling needed here.

### Out of scope

`GET /v1/model_registry` (the admin listing endpoint) currently has no `require_admin` guard. Noted during exploration, but unrelated to this fix — the new `/usable` endpoint is deliberately scoped to safe, filtered, masked data for regular users regardless of that gap.

### Testing

- Backend: unit test the `/v1/profiles/llm-options` filter logic — enabled/disabled, stable/testing × `can_use_testing` true/false.
- Backend: extend/verify `_validate_profile_models()` coverage for a profile saved with a registry-sourced `llm.engine`/`llm.model` that becomes disabled or testing-gated before save.
- Frontend: manual browser check (dev server) — open an existing custom profile (unaffected), open/create a profile and pick a registry entry, save, reopen to confirm round-trip; verify a disabled/testing entry doesn't appear for a non-testing user.
