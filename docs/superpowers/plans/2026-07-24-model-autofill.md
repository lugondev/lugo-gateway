# Model ID auto-fill (single-model → auto; multi-model → pick)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Stop making the admin type `model_id` when there's only one model to use. Unify the model-id combobox to load choices from BOTH sources — a provider's `/models` (remote) OR the selected engine's configured default (local) — and **auto-fill when there's exactly one choice**. Only when there are many (a cloud provider like OpenRouter) does the admin search/pick. Manual typing always still works.

**Why (verified in code):**
- Self-hosted `apps/model_service` = one engine/container → its `/v1/models` returns exactly ONE model (`id = config.engine`). Cloud (OpenAI/OpenRouter) `/models` returns many.
- Local single-model engines (`qwen3_asr_gguf`, `vosk`) ignore `model_id`'s value (they load from a config path); `whisper`/`qwen3_asr` have variants. `model_id` can't be removed (it's the entry's selection key + needed for multi-model/cloud) but for the single cases the admin shouldn't have to type it.

**Architecture:** Static-UI only, one file: `apps/api_gateway/app/static/js/model-registry.js`. Rework the existing model combobox: rename/extend `_loadProviderModels` → `_loadModelChoices` (two sources + auto-fill), fold it into the end of `_loadEngineOptions`, add an engine-`<select>` change listener, track `_lastAutoFilled`. Endpoints already exist: `GET /v1/providers/{id}/models` and `GET /v1/model_registry/config_schema?kind=&engine=` (→ `{fields:[{key,type,default}]}`; the model field is `default_model` for whisper/qwen3_asr/qwen3_asr_gguf, `model_path` for vosk/whisper_mlx).

## Global Constraints
- Static-UI only. Verify `node --check` + grep; NO pytest. No backend change.
- Auto-fill ONLY when exactly one choice AND the admin hasn't typed their own value (track `_lastAutoFilled`; user `input` event clears it). Never clobber manual input.
- Manual free-text entry stays (combobox is non-restrictive; `createModelRegistryEntry` still reads `el("registry-add-model-id").value.trim()` — unchanged).
- Sources: provider selected → `/v1/providers/{id}/models` (`.data.models`); else local stt/tts with an engine selected → `config_schema` field `default_model`||`model_path` `.default` (a single value). Use the raw default value (don't transform paths).
- Git `lugondev <lugondev@gmail.com>`. Concurrent session — re-check branch before git. No push (main auto-deploys prod).

---

### Task 1: Unify model-choice loading + auto-fill-when-one

**Files:** Modify `apps/api_gateway/app/static/js/model-registry.js`.

- [ ] **Step 1: Read** the current combobox block: `_modelChoices`, `_loadProviderModels`, `_renderModelPanel`, `_openModelPanel`, `_closeModelPanel`, the input listeners, the `registry-add-provider` change handler, and `_loadEngineOptions`. Confirm `createModelRegistryEntry` reads the model input value (leave it).

- [ ] **Step 2: Add `_lastAutoFilled` + replace `_loadProviderModels` with `_loadModelChoices`.** Keep the `let _modelChoices = [];` line; add `let _lastAutoFilled = null;` next to it. Replace the whole `_loadProviderModels` function with:
```javascript
async function _loadModelChoices() {
  _modelChoices = [];
  const input = el("registry-add-model-id");
  const status = el("model-registry-status");
  const providerId = (el("registry-add-provider")?.value || "").trim();
  const kind = el("registry-add-kind")?.value;
  try {
    if (providerId) {
      // Remote: the provider's advertised models (self-hosted service returns 1;
      // a cloud provider returns many).
      const body = await (await fetch(`/v1/providers/${encodeURIComponent(providerId)}/models`)).json();
      _modelChoices = (body.data && body.data.models) || [];
      if (body.data && body.data.error) {
        print(status, `Couldn't load models (${body.data.error}) — type the model id manually.`, true);
      }
    } else if (kind === "stt" || kind === "tts") {
      // Local: the selected engine's single configured model (default_model / model_path).
      const engine = (el("registry-add-engine")?.value || "").trim();
      if (engine) {
        const body = await (await fetch(`/v1/model_registry/config_schema?kind=${encodeURIComponent(kind)}&engine=${encodeURIComponent(engine)}`)).json();
        const f = (body.fields || []).find((x) => x.key === "default_model" || x.key === "model_path");
        if (f && f.default) _modelChoices = [String(f.default)];
      }
    }
  } catch (e) {
    print(status, `Couldn't load models (${e}) — type the model id manually.`, true);
  }
  // Auto-fill when there's exactly ONE model (self-hosted 1-model service, or a
  // single-model local engine). Don't clobber a value the admin typed themselves.
  if (input) {
    const canAuto = input.value === "" || input.value === _lastAutoFilled;
    if (_modelChoices.length === 1 && canAuto) {
      input.value = _modelChoices[0];
      _lastAutoFilled = _modelChoices[0];
      if (status) status.textContent = `Model set to "${_modelChoices[0]}".`;
    } else if (input.value === "") {
      _lastAutoFilled = null;
    }
  }
  _renderModelPanel();
}
```

- [ ] **Step 3: Fold model-loading into `_loadEngineOptions` + drop the old separate call.** At the very END of `_loadEngineOptions` (after both the early-return-hide path AND the local-populate path — so put it as the last statement before the function returns in BOTH branches, or restructure so it always runs at the end), call `void _loadModelChoices();`. Simplest: since the llm/provider branch `return`s early, add `void _loadModelChoices();` right before each `return`/at the end. Concretely: in the `if (kind === "llm" || hasProvider) { ...hide...; void _loadModelChoices(); return; }` branch add the call before `return`, and add it as the last line of the function for the local branch.
  Then in the `registry-add-provider` change listener, REMOVE the now-redundant explicit `void _loadProviderModels();` (it's covered by `_loadEngineOptions` → `_loadModelChoices`). The listener keeps `_updateKindFields()` + `void _loadEngineOptions()`.

- [ ] **Step 4: Add an engine-select change listener** (so manually picking a different LOCAL engine reloads its default model). Near the other bottom listeners:
```javascript
if (el("registry-add-engine")) {
  el("registry-add-engine").addEventListener("change", () => { void _loadModelChoices(); });
}
```

- [ ] **Step 5: Clear `_lastAutoFilled` on manual typing.** In the model input's existing `input` listener, change `() => { _openModelPanel(); }` to `() => { _lastAutoFilled = null; _openModelPanel(); }` (user took over → future auto-fills won't overwrite).

- [ ] **Step 6: Rename remaining refs.** Ensure NO remaining `_loadProviderModels` references anywhere (grep) — all replaced by `_loadModelChoices`.

- [ ] **Step 7: Verify** — `node --check apps/api_gateway/app/static/js/model-registry.js` (OK). Grep: `_loadModelChoices` defined + called from `_loadEngineOptions` + engine-change listener; zero `_loadProviderModels`; `_lastAutoFilled` set in the auto-fill + cleared in the input listener. Reasoning walkthrough (state in report):
  - Provider = self-hosted service (/models→1) → model_id auto-fills that 1.
  - Provider = OpenRouter (/models→many) → no auto-fill; combobox to search.
  - No provider + engine=qwen3_asr_gguf → config default_model (1) → auto-fills.
  - No provider + engine=whisper → default "large-v3-turbo" auto-fills, editable.
  - Admin types a custom id → `_lastAutoFilled` cleared → not overwritten on further changes.

- [ ] **Step 8: Commit** — model-registry.js → `feat(admin-ui): auto-fill model id when a single model (service/local); search when many`.

---

### Task 2: Verify (controller)
- [ ] `node --check apps/api_gateway/app/static/js/model-registry.js`; grep the new logic; `.venv/bin/python -c "import app.main"` (unaffected).

## Self-Review
- **Coverage:** model_id no longer typed when there's one model — auto-filled from the provider's single `/models` entry (self-hosted service) or the local engine's configured default; multi-model (cloud/whisper) keeps the searchable combobox; manual entry preserved (with no-clobber). Directly answers "engine/service is already one model → why type model_id."
- **Placeholders:** complete code.
- **Consistency:** `_loadModelChoices` reads provider `.data.models` (matches /v1/providers/{id}/models) and config_schema `fields[].default` (matches /config_schema); folded into `_loadEngineOptions` so kind/provider changes refresh it; engine-select change wired; `_lastAutoFilled` no-clobber; createModelRegistryEntry unchanged.
