# Model Registry Add-Form: Engine select + Model searchable combobox

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Replace the free-text ENGINE and MODEL ID inputs in the Model Registry "Add Entry" form with proper pickers: Engine → a `<select>` of valid engines for the chosen kind (auto-derived + hidden for llm since it's cosmetic); Model ID → a searchable dropdown (combobox) populated from the selected provider's models, still accepting a typed value for models the provider doesn't list.

**Why:** Engine is a dispatch key (stt/tts pick the adapter class; llm is a cosmetic label). Typing internal names (`qwen3_asr_or`) free-hand is error-prone; a select removes that. Model ids should be discoverable from the provider, searchable (OpenRouter lists ~300), but not locked (self-hosted providers may not list models).

**Architecture:** Static-UI only (`apps/api_gateway/app/static`). Backend endpoints already exist: `GET /v1/stt/engines` + `GET /v1/tts/engines` (→ `{success, data:[{engine, available, ...}]}`), `GET /v1/providers/{id}/models` (→ `{success, data:{models:[str], error}}`), `GET /v1/providers`. All work happens in `index.html`, `styles.css`, and `model-registry.js` (rework of the add-form handlers). No React change (registry mgmt is admin-console only).

## Global Constraints
- Static-UI only — NO Python, NO backend change. Verify: `node --check apps/api_gateway/app/static/js/model-registry.js` + grep; NO pytest.
- **Engine = `<select>`, never free text.** stt/tts: options = engines from `/v1/{kind}/engines`. llm: engine is cosmetic → auto-derive value = selected provider's `name` (or `"custom"` if no provider) AND hide the Engine field.
- **Model ID = searchable combobox**: a text input + a filtered dropdown panel of the provider's models; click-to-pick; typing filters; a typed value not in the list is still accepted (custom) — do NOT hard-restrict, or models the provider doesn't enumerate become un-addable.
- Preserve existing behavior: provider-linked entries send `config.provider_id` (no base_url/api_key); non-provider entries keep the per-kind base_url/api_key logic; `_updateKindFields` still hides the credential rows when a provider is selected.
- Git `lugondev <lugondev@gmail.com>`. Concurrent session active — re-check `git branch --show-current` before git steps. No push.

---

### Task 1: ENGINE → `<select>` (per-kind options; llm auto-derived + hidden)

**Files:** Modify `apps/api_gateway/app/static/index.html` + `apps/api_gateway/app/static/js/model-registry.js`.

- [ ] **Step 1: index.html** — change the Engine field from an `<input>` to a `<select>`, and give its wrapping `<label>` an id so it can be hidden. Current (~line 907-910):
```html
                <label>
                  Engine
                  <input id="registry-add-engine" type="text" placeholder="qwen3_asr_or (stt) · vieneu (tts) · openai (llm)" />
                </label>
```
Replace with:
```html
                <label id="registry-add-engine-wrap">
                  Engine
                  <select id="registry-add-engine"></select>
                </label>
```

- [ ] **Step 2: model-registry.js — add `_loadEngineOptions`** (place near `_loadProviderOptions`). It populates the engine `<select>` for the current kind, or hides the field for llm:
```javascript
// Engine is a dispatch key for stt/tts (picks the adapter); for llm it's a
// cosmetic label, so we derive it from the provider and hide the field.
async function _loadEngineOptions() {
  const kind = el("registry-add-kind")?.value;
  const sel = el("registry-add-engine");
  const wrap = el("registry-add-engine-wrap");
  if (!sel) return;
  if (kind === "llm") {
    if (wrap) wrap.classList.add("hidden"); // engine derived from provider on submit
    return;
  }
  if (wrap) wrap.classList.remove("hidden");
  const prev = sel.value;
  sel.innerHTML = "";
  try {
    const body = await (await fetch(`/v1/${kind}/engines`)).json();
    const engines = (body.data || []);
    for (const item of engines) {
      const opt = document.createElement("option");
      opt.value = item.engine;
      opt.textContent = item.available ? item.engine : `${item.engine} (unavailable)`;
      sel.appendChild(opt);
    }
    if (engines.some((e) => e.engine === prev)) sel.value = prev; // keep selection across kind re-renders
  } catch {
    /* leave empty; submit will surface a clear error */
  }
}
```

- [ ] **Step 3: model-registry.js — derive engine for llm + use the select in `createModelRegistryEntry`.** Add a helper and rewire. Add near the top helpers:
```javascript
// llm engine is cosmetic; use the linked provider's name as the label (or "custom").
function _effectiveEngine() {
  const kind = el("registry-add-kind")?.value;
  if (kind !== "llm") return (el("registry-add-engine")?.value || "").trim();
  const sel = el("registry-add-provider");
  const name = sel?.selectedOptions?.[0]?.textContent || "";
  // provider option text is `name — label`; take the name part; fallback "custom"
  return (name.split(" — ")[0] || "custom").trim() || "custom";
}
```
In `createModelRegistryEntry`, replace `const engine = el("registry-add-engine").value.trim();` with `const engine = _effectiveEngine();`. Keep the `if (!engine || !modelId || !label)` guard (for stt/tts a blank engine means the select had no options → a real error; for llm `_effectiveEngine` never returns blank).

- [ ] **Step 4: model-registry.js — wire `_loadEngineOptions` into kind + provider changes + initial load.**
  - In the `registry-add-kind` change listener (currently `addEventListener("change", _updateKindFields)`), also call `void _loadEngineOptions()`. And call `void _loadEngineOptions()` once on init (next to the initial `_updateKindFields()`).
  - In the `registry-add-provider` change listener, also call `void _loadEngineOptions()` (so switching to a provider while kind=llm keeps the hide correct — harmless for stt/tts).
  Concretely rewrite the bottom block:
```javascript
if (el("registry-add-kind")) {
  el("registry-add-kind").addEventListener("change", () => {
    _updateKindFields();
    void _loadEngineOptions();
  });
  _updateKindFields();
  void _loadEngineOptions();
}
if (el("registry-add-provider")) {
  el("registry-add-provider").addEventListener("change", () => {
    _updateKindFields();
    void _loadEngineOptions();
    void _loadProviderModelSuggestions();
  });
}
```
  (`_loadProviderModelSuggestions` stays for now — Task 2 replaces it.)

- [ ] **Step 5: reset on success** — in `createModelRegistryEntry`'s success branch, the line `el("registry-add-engine").value = "";` now targets a `<select>`; that's harmless (sets to "" → no matching option → shows first), but prefer to re-run `void _loadEngineOptions();` after a successful add instead of setting `.value = ""`. Replace that one reset line accordingly (remove `el("registry-add-engine").value = "";`; the subsequent `loadModelRegistry()` + `_loadEngineOptions` re-render covers it — or call `_loadEngineOptions()` explicitly).

- [ ] **Step 6: Verify** — `node --check apps/api_gateway/app/static/js/model-registry.js` (OK); grep confirms `<select id="registry-add-engine">` in index.html and `_loadEngineOptions` wired. Confirm no remaining `registry-add-engine` as an `<input>`.

- [ ] **Step 7: Commit** — index.html + model-registry.js → `feat(admin-ui): Engine as a per-kind select (auto-derived+hidden for llm)`.

---

### Task 2: MODEL ID → searchable combobox (replaces the native datalist)

**Files:** Modify `apps/api_gateway/app/static/index.html`, `apps/api_gateway/app/static/styles.css`, `apps/api_gateway/app/static/js/model-registry.js`.

- [ ] **Step 1: index.html** — replace the Model ID input+datalist with a combobox container. Current:
```html
                <label>
                  Model ID
                  <input id="registry-add-model-id" type="text" placeholder="qwen3-asr-flash" list="registry-model-suggestions" autocomplete="off" />
                  <datalist id="registry-model-suggestions"></datalist>
                </label>
```
Replace with:
```html
                <label>
                  Model ID
                  <div class="combobox" id="registry-model-combo">
                    <input id="registry-add-model-id" type="text" placeholder="pick or type a model id" autocomplete="off" role="combobox" aria-expanded="false" aria-autocomplete="list" />
                    <ul id="registry-model-options" class="combobox-panel" hidden></ul>
                  </div>
                </label>
```

- [ ] **Step 2: styles.css** — append combobox styles (mirror the app's dark theme; reuse existing color vars if present — grep `--` tokens in styles.css and use a card/border token, else the literals below):
```css
.combobox { position: relative; }
.combobox-panel {
  position: absolute; left: 0; right: 0; top: 100%; z-index: 40;
  margin: 2px 0 0; padding: 4px 0; list-style: none;
  max-height: 240px; overflow-y: auto;
  background: #0d1117; border: 1px solid #223; border-radius: 6px;
  box-shadow: 0 8px 24px rgba(0,0,0,0.4);
}
.combobox-panel[hidden] { display: none; }
.combobox-panel li {
  padding: 6px 12px; cursor: pointer; font-size: 0.9em; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis;
}
.combobox-panel li:hover, .combobox-panel li.active { background: #1b2432; }
.combobox-empty { padding: 6px 12px; opacity: 0.6; font-size: 0.85em; }
```

- [ ] **Step 3: model-registry.js — replace `_loadProviderModelSuggestions` with the combobox.** Remove the old `_loadProviderModelSuggestions` function and add:
```javascript
let _modelChoices = []; // model ids fetched from the selected provider

async function _loadProviderModels() {
  _modelChoices = [];
  const providerId = (el("registry-add-provider")?.value || "").trim();
  const status = el("model-registry-status");
  if (!providerId) { _renderModelPanel(); return; }
  try {
    const resp = await fetch(`/v1/providers/${encodeURIComponent(providerId)}/models`);
    const body = await resp.json();
    _modelChoices = (body.data && body.data.models) || [];
    if (body.data && body.data.error) {
      print(status, `Couldn't load models (${body.data.error}) — type the model id manually.`, true);
    } else if (_modelChoices.length && status) {
      status.textContent = `Loaded ${_modelChoices.length} model(s) — pick or type.`;
    }
  } catch (e) {
    print(status, `Couldn't load models (${e}) — type the model id manually.`, true);
  }
  _renderModelPanel();
}

function _renderModelPanel() {
  const panel = el("registry-model-options");
  const input = el("registry-add-model-id");
  if (!panel || !input) return;
  const q = input.value.trim().toLowerCase();
  const matches = _modelChoices.filter((m) => !q || m.toLowerCase().includes(q)).slice(0, 100);
  if (!matches.length) {
    panel.innerHTML = _modelChoices.length
      ? `<li class="combobox-empty">No match — press Enter to use "${escapeHtml(input.value.trim())}"</li>`
      : `<li class="combobox-empty">No models listed — type the model id.</li>`;
  } else {
    panel.innerHTML = matches.map((m) => `<li data-model="${escapeHtml(m)}">${escapeHtml(m)}</li>`).join("");
  }
}

function _openModelPanel() {
  const panel = el("registry-model-options");
  const input = el("registry-add-model-id");
  if (!panel || !input) return;
  _renderModelPanel();
  panel.hidden = false;
  input.setAttribute("aria-expanded", "true");
}

function _closeModelPanel() {
  const panel = el("registry-model-options");
  const input = el("registry-add-model-id");
  if (panel) panel.hidden = true;
  if (input) input.setAttribute("aria-expanded", "false");
}
```

- [ ] **Step 4: model-registry.js — wire the combobox events** (add to the bottom listeners block; guard each with `el(...)`):
```javascript
if (el("registry-add-model-id")) {
  const input = el("registry-add-model-id");
  input.addEventListener("focus", _openModelPanel);
  input.addEventListener("input", () => { _openModelPanel(); }); // re-filter as they type
  input.addEventListener("keydown", (e) => { if (e.key === "Escape") _closeModelPanel(); });
}
if (el("registry-model-options")) {
  el("registry-model-options").addEventListener("mousedown", (e) => {
    // mousedown (not click) so it fires before the input's blur
    const li = e.target.closest("li[data-model]");
    if (!li) return;
    e.preventDefault();
    el("registry-add-model-id").value = li.getAttribute("data-model");
    _closeModelPanel();
  });
}
// click outside closes the panel
document.addEventListener("click", (e) => {
  const combo = el("registry-model-combo");
  if (combo && !combo.contains(e.target)) _closeModelPanel();
});
```

- [ ] **Step 5: replace the old datalist call sites.** In the `registry-add-provider` change listener, replace `void _loadProviderModelSuggestions();` with `void _loadProviderModels();`. Ensure no remaining references to `_loadProviderModelSuggestions` or `registry-model-suggestions` anywhere (grep).

- [ ] **Step 6: Verify** — `node --check apps/api_gateway/app/static/js/model-registry.js` (OK); grep: `registry-model-options` + `registry-model-combo` present in index.html & JS; ZERO remaining `registry-model-suggestions` / `_loadProviderModelSuggestions`; `.combobox-panel` present in styles.css. Confirm the model input still accepts free text (value read in `createModelRegistryEntry` unchanged: `el("registry-add-model-id").value.trim()`).

- [ ] **Step 7: Commit** — index.html + styles.css + model-registry.js → `feat(admin-ui): searchable model-id combobox (provider models + manual)`.

---

### Task 3: Verify (controller)
- [ ] `node --check apps/api_gateway/app/static/js/model-registry.js`; grep the new ids present + old datalist refs gone; `.venv/bin/python -c "import app.main"` (unaffected — no Python change).

## Self-Review
- **Coverage:** Engine → select (stt/tts from /engines; llm auto-derived+hidden) removes free-text engine entry; Model ID → searchable combobox from provider /models with manual fallback. Both were the user's asks ("engine dropdown/select, không điền tự do"; "model dropdown có search").
- **Placeholders:** complete code for both tasks; Task 2 combobox is a self-contained vanilla component.
- **Consistency:** engine `<select id="registry-add-engine">` read via `_effectiveEngine()` in createModelRegistryEntry; combobox ids (`registry-model-combo`/`registry-model-options`) consistent between index.html & JS; provider-change wires _loadEngineOptions + _loadProviderModels; createModelRegistryEntry still sends `config.provider_id` for provider-linked and per-kind base_url/api_key otherwise.
