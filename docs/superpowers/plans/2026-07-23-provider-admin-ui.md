# Provider Admin UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add an admin UI to manage Providers (from Phase 1's `/v1/providers`) and to link a Model Registry entry to a provider — so the whole "configure endpoint+key once, reuse across models" flow works from the browser, no hand-edited JSON.

**Architecture:** Static ES-module admin UI (`apps/api_gateway/app/static`). New `providers.js` mirrors the existing `model-registry.js` pattern (fetch → `renderDataTable` → add-form + status). A new sidebar section `providers` (admin-only) is wired the same way every other section is (`data-section` nav-item + `#section-<name>` + a `loadX()` call in `sidebar-nav.js`, module imported for side effects in `main.js`). Task 2 adds a Provider `<select>` to the Model Registry add-form that sets `config.provider_id`.

**Tech Stack:** Vanilla ES modules, `helpers.js` (`el`, `print`, `escapeHtml`), `data-table.js` (`renderDataTable`), `modal.js` (`confirmDialog`). Backend endpoints already exist and are tested: `GET/POST /v1/providers`, `PATCH/DELETE /v1/providers/{id}`, `GET /v1/providers/presets`.

## Global Constraints

- **Static-UI change only** — no Python edits. Verification is `node --check <file>` for JS syntax + a grep for dangling references + (optional) a browser smoke check. Do NOT run the backend pytest suite for these edits (per repo convention: full suite is a pre-commit gate, never for static-UI edits).
- Admin-gated: the `/v1/providers` routes are already admin-only (backend `_ADMIN_PREFIXES`); the nav-item must carry the `admin-only` class like `users`/`model-registry` so non-admins never see the tab.
- `api_key` is returned **masked** by the backend — the UI must NEVER try to display a real key, and the edit field is write-only (blank = keep existing), exactly like `model-registry.js`'s `data-detail-apikey`.
- Follow the existing DOM/class vocabulary verbatim: `section`, `card`, `card-head`, `row tight`, `hint`, `meta`, `mini`, `ghost`, `actions end`, `nav-item`, `admin-only`.
- Git identity: `lugondev <lugondev@gmail.com>`. Do NOT touch submodules/.dockerignore. Do NOT push (main auto-deploys prod).

---

### Task 1: Providers management tab (list + add-with-presets + edit/delete/toggle)

**Files:**
- Create: `apps/api_gateway/app/static/js/providers.js`
- Modify: `apps/api_gateway/app/static/index.html` (add nav-item after the `model-registry` one ~line 106-111; add `<section id="section-providers">` after `#section-model-registry` closes ~line 916)
- Modify: `apps/api_gateway/app/static/js/sidebar-nav.js` (import + activation call)
- Modify: `apps/api_gateway/app/static/js/main.js` (side-effect import)

**Interfaces:**
- Produces: `export async function loadProviders()` — fetch `/v1/providers`, render into `#providers-list`. Also exports nothing else consumed elsewhere (add-form handlers attach at module load, like `model-registry.js`).

- [ ] **Step 1: Add the nav-item** in `index.html`, immediately AFTER the model-registry nav-item block (the `</li>`-wrapped `<button ... data-section="model-registry">`, ~line 106-111). Match its exact structure (it's an `admin-only` item). Insert:

```html
              <button class="nav-item admin-only" data-section="providers">
                <span class="nav-icon">🔌</span>
                <span class="nav-label">Providers</span>
              </button>
```
(If the sibling nav-items are wrapped in `<li>` or use a different icon/label markup, mirror THAT exact structure — copy the model-registry nav-item and change only `data-section`, icon, and label. Read lines 106-112 first.)

- [ ] **Step 2: Add the section markup** in `index.html`, immediately AFTER `#section-model-registry`'s closing `</div>` (~line 916, before the `<!-- SYSTEM -->` comment):

```html
          <!-- ============================== PROVIDERS ============================== -->
          <div class="section" id="section-providers">
            <section class="card">
              <div class="card-head">
                <h2>Providers</h2>
                <button id="providers-refresh" class="ghost mini">Refresh</button>
              </div>
              <p class="hint">Configure an OpenAI-compatible provider (endpoint + API key) once, then link models to it in Model Registry instead of repeating the key per model.</p>
              <div id="providers-list" class="model-list">
                <p class="hint">Loading&#8230;</p>
              </div>
              <h3 class="sub">Add Provider</h3>
              <div class="row tight">
                <label>
                  Preset
                  <select id="provider-add-preset">
                    <option value="">— custom —</option>
                  </select>
                </label>
                <label>
                  Name
                  <input id="provider-add-name" type="text" placeholder="openai · openrouter · qwencloud" />
                </label>
                <label>
                  Label
                  <input id="provider-add-label" type="text" placeholder="OpenAI" />
                </label>
              </div>
              <div class="row tight">
                <label>
                  Base URL
                  <input id="provider-add-base-url" type="text" placeholder="https://api.openai.com/v1" />
                </label>
                <label>
                  API Key
                  <input id="provider-add-api-key" type="password" placeholder="sk-…" autocomplete="off" />
                </label>
              </div>
              <div class="actions end">
                <button id="provider-add-btn">Add Provider</button>
              </div>
              <p id="providers-status" class="meta"></p>
            </section>
          </div>
```

- [ ] **Step 3: Create `providers.js`** with the full content below:

```javascript
import { el, print, escapeHtml } from "./helpers.js";
import { renderDataTable } from "./data-table.js";
import { confirmDialog } from "./modal.js";

export let providerData = [];
let presetData = [];

export async function loadProviders() {
  try {
    const body = await (await fetch("/v1/providers")).json();
    providerData = body.data || [];
    renderProviders();
  } catch {
    /* ignore */
  }
  await _loadPresets();
}

async function _loadPresets() {
  if (presetData.length) return;
  const sel = el("provider-add-preset");
  if (!sel) return;
  try {
    const body = await (await fetch("/v1/providers/presets")).json();
    presetData = body.data || [];
  } catch {
    return;
  }
  for (const p of presetData) {
    const opt = document.createElement("option");
    opt.value = p.name;
    opt.textContent = p.label || p.name;
    sel.appendChild(opt);
  }
}

function renderProviders() {
  const host = el("providers-list");
  if (!host) return;
  host.innerHTML = "";
  const table = renderDataTable({
    container: host,
    rows: providerData,
    rowKey: (p) => p.id,
    emptyMessage: "No providers yet — add one below.",
    getRowClass: (p) => (p.enabled ? "" : "dim"),
    columns: [
      { key: "name", label: "Name", render: (p) => `<strong>${escapeHtml(p.name)}</strong>${p.label ? ` <span class="hint">${escapeHtml(p.label)}</span>` : ""}` },
      { key: "base_url", label: "Base URL", render: (p) => `<code>${escapeHtml(p.base_url || "—")}</code>` },
      { key: "api_key", label: "API Key", render: (p) => `<code class="hint">${escapeHtml(p.api_key || "not set")}</code>` },
      {
        key: "actions",
        label: "",
        headerClass: "dt-actions-cell",
        cellClass: "dt-actions-cell",
        render: (p) => `
          <button class="mini" data-provider-edit="${escapeHtml(p.id)}">Edit</button>
          <button class="mini" data-provider-toggle="${escapeHtml(p.id)}">${p.enabled ? "Disable" : "Enable"}</button>
          <button class="mini danger" data-provider-delete="${escapeHtml(p.id)}">Delete</button>
        `,
      },
    ],
    rowDetail: (p) => `
      <div class="registry-detail" data-provider-detail="${escapeHtml(p.id)}">
        <label class="registry-field">
          <span>Label</span>
          <input type="text" class="mini" data-detail-label value="${escapeHtml(p.label || "")}" />
        </label>
        <label class="registry-field">
          <span>Base URL</span>
          <input type="text" class="mini" data-detail-baseurl value="${escapeHtml(p.base_url || "")}" placeholder="https://…" />
        </label>
        <label class="registry-field">
          <span>API Key</span>
          <code class="hint">${escapeHtml(p.api_key || "not set")}</code>
          <input type="password" class="mini" data-detail-apikey placeholder="new key… (blank = keep)" autocomplete="off" />
        </label>
        <button class="mini" data-provider-save="${escapeHtml(p.id)}">Save</button>
      </div>`,
  });
  if (!table) return;

  table.querySelectorAll("[data-provider-toggle]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-provider-toggle");
      const p = providerData.find((x) => x.id === id);
      patchProvider(id, { enabled: !p.enabled });
    })
  );
  table.querySelectorAll("[data-provider-delete]").forEach((btn) =>
    btn.addEventListener("click", () => deleteProvider(btn.getAttribute("data-provider-delete")))
  );
  table.querySelectorAll("[data-provider-edit]").forEach((btn) =>
    btn.addEventListener("click", () => table.toggleDetail(btn.getAttribute("data-provider-edit")))
  );
  table.querySelectorAll("[data-provider-save]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-provider-save");
      const detail = document.querySelector(`[data-provider-detail="${CSS.escape(id)}"]`);
      const fields = {
        label: detail.querySelector("[data-detail-label]").value.trim(),
        base_url: detail.querySelector("[data-detail-baseurl]").value.trim(),
      };
      const key = detail.querySelector("[data-detail-apikey]").value.trim();
      if (key) fields.api_key = key; // blank = keep existing
      patchProvider(id, fields);
    })
  );
}

async function patchProvider(id, fields) {
  try {
    const resp = await fetch(`/v1/providers/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(fields),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      print(el("providers-status"), body.detail || "Update failed", true);
      return;
    }
    await loadProviders();
  } catch (error) {
    print(el("providers-status"), String(error), true);
  }
}

async function deleteProvider(id) {
  const p = providerData.find((x) => x.id === id);
  if (!(await confirmDialog(`Delete provider "${p?.name || id}"? Models linked to it will fall back to their own credentials.`, { danger: true }))) return;
  try {
    const resp = await fetch(`/v1/providers/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      print(el("providers-status"), body.detail || "Delete failed", true);
      return;
    }
    await loadProviders();
  } catch (error) {
    print(el("providers-status"), String(error), true);
  }
}

export async function createProvider() {
  const status = el("providers-status");
  const name = el("provider-add-name").value.trim();
  const label = el("provider-add-label").value.trim();
  const baseUrl = el("provider-add-base-url").value.trim();
  const apiKey = el("provider-add-api-key").value.trim();
  if (!name) {
    print(status, "Enter a provider name", true);
    return;
  }
  status.textContent = "Adding…";
  try {
    const resp = await fetch("/v1/providers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, label, base_url: baseUrl, api_key: apiKey }),
    });
    const body = await resp.json();
    if (!resp.ok) {
      print(status, body.detail || "Add failed", true);
      return;
    }
    status.textContent = `Added "${name}"`;
    el("provider-add-name").value = "";
    el("provider-add-label").value = "";
    el("provider-add-base-url").value = "";
    el("provider-add-api-key").value = "";
    if (el("provider-add-preset")) el("provider-add-preset").value = "";
    await loadProviders();
  } catch (error) {
    print(status, String(error), true);
  }
}

// Preset select auto-fills name + label + base_url (all still editable).
function _applyPreset() {
  const name = el("provider-add-preset").value;
  if (!name) return;
  const preset = presetData.find((p) => p.name === name);
  if (!preset) return;
  el("provider-add-name").value = preset.name;
  el("provider-add-label").value = preset.label || preset.name;
  el("provider-add-base-url").value = preset.base_url || "";
}

if (el("provider-add-preset")) el("provider-add-preset").addEventListener("change", _applyPreset);
if (el("provider-add-btn")) el("provider-add-btn").addEventListener("click", createProvider);
if (el("providers-refresh")) el("providers-refresh").addEventListener("click", loadProviders);
```

- [ ] **Step 4: Wire `sidebar-nav.js`** — add the import and the activation call, mirroring `model-registry`:

```javascript
import { loadProviders } from "./providers.js";
```
and inside `activateSection`, after the `model-registry` line:
```javascript
  if (section === "providers") loadProviders();
```

- [ ] **Step 5: Wire `main.js`** — add alongside the other side-effect imports (after `import "./model-registry.js";`):

```javascript
import "./providers.js";
```

- [ ] **Step 6: Verify (static-UI checks — NOT pytest)**

Run:
```bash
cd /Users/lugon/code/speech-text-transformer
node --check apps/api_gateway/app/static/js/providers.js && echo "providers.js OK"
node --check apps/api_gateway/app/static/js/sidebar-nav.js && echo "sidebar-nav.js OK"
node --check apps/api_gateway/app/static/js/main.js && echo "main.js OK"
grep -n "section-providers\|data-section=\"providers\"" apps/api_gateway/app/static/index.html
```
Expected: all three "OK"; grep shows both the nav-item and the section present.

- [ ] **Step 7: Commit**

```bash
git add apps/api_gateway/app/static/js/providers.js apps/api_gateway/app/static/index.html apps/api_gateway/app/static/js/sidebar-nav.js apps/api_gateway/app/static/js/main.js
git -c user.name=lugondev -c user.email=lugondev@gmail.com commit -m "feat(admin-ui): Providers management tab (list + presets + CRUD)"
```

---

### Task 2: Link a Model Registry entry to a provider (add-form dropdown)

**Files:**
- Modify: `apps/api_gateway/app/static/index.html` (add a Provider `<select>` to the model-registry add-form, ~line 866-894 area)
- Modify: `apps/api_gateway/app/static/js/model-registry.js` (populate the select; put `provider_id` into `payload.config`; relax base_url/api_key requirement when a provider is chosen)

**Interfaces:**
- Consumes: `GET /v1/providers` (list, for the dropdown). Task 1's backend already returns it.
- Produces: when a provider is selected, `createModelRegistryEntry()` sends `config: { provider_id: <id> }` and leaves base_url/api_key blank (backend resolves creds from the provider at test-time and read-time — Phase 1).

- [ ] **Step 1: Add the Provider select** to the model-registry add-form in `index.html`. Inside the first `.row tight` add-entry block (the one containing Kind/Engine/Model ID/Label/Stage, ~line 866-894), add as the FIRST label so it reads left-to-right "Provider → Kind → Engine …":

```html
                <label>
                  Provider
                  <select id="registry-add-provider">
                    <option value="">— none (use fields below) —</option>
                  </select>
                </label>
```

- [ ] **Step 2: Populate the select + use it** — edit `model-registry.js`:

  (a) Add a populate function and call it from `loadModelRegistry()` (right after `renderModelRegistry();`):

```javascript
async function _loadProviderOptions() {
  const sel = el("registry-add-provider");
  if (!sel) return;
  try {
    const body = await (await fetch("/v1/providers")).json();
    const providers = (body.data || []).filter((p) => p.enabled);
    // rebuild, keeping the leading "none" option
    sel.length = 1;
    for (const p of providers) {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = p.label ? `${p.name} — ${p.label}` : p.name;
      sel.appendChild(opt);
    }
  } catch {
    /* ignore — dropdown just stays at "none" */
  }
}
```
In `loadModelRegistry()`:
```javascript
    renderModelRegistry();
    await _loadProviderOptions();
```

  (b) In `createModelRegistryEntry()`, read the selected provider and adjust the payload. Replace the existing `const payload = { kind, engine, model_id: modelId, label, stage };` line and the credential block with:

```javascript
  const providerId = (el("registry-add-provider")?.value || "").trim();
  const payload = { kind, engine, model_id: modelId, label, stage };
  if (providerId) {
    // Linked to a provider: creds come from the provider row; leave base_url/
    // api_key blank and stash the link in config so the backend resolves them.
    payload.config = { provider_id: providerId };
  } else if (kind === "llm" || kind === "stt") {
    payload.base_url = el("registry-add-base-url").value.trim();
    payload.api_key = el("registry-add-api-key").value.trim();
  } else {
    payload.base_url = el("registry-add-base-url").value.trim();
    payload.api_key = el("registry-add-key-api-key").value.trim();
  }
```

  (c) Reset the select after a successful add (in the success branch, alongside the other field resets):
```javascript
    if (el("registry-add-provider")) el("registry-add-provider").value = "";
```

  (d) Make the credential rows visually optional when a provider is picked: extend `_updateKindFields` so a provider selection hides the raw credential rows. Add a change listener and update the function:
```javascript
function _updateKindFields() {
  const kind = el("registry-add-kind").value;
  const hasProvider = !!(el("registry-add-provider")?.value || "").trim();
  const isLlmOrStt = kind === "llm" || kind === "stt";
  el("registry-add-llm-fields").classList.toggle("hidden", hasProvider || !(isLlmOrStt || kind === "tts"));
  el("registry-add-llm-apikey-wrap").classList.toggle("hidden", kind === "tts");
  el("registry-add-key-fields").classList.toggle("hidden", hasProvider || isLlmOrStt);
}
```
and register the listener near the bottom, next to the existing `registry-add-kind` change binding:
```javascript
if (el("registry-add-provider")) el("registry-add-provider").addEventListener("change", _updateKindFields);
```

- [ ] **Step 3: Verify (static-UI checks)**

```bash
cd /Users/lugon/code/speech-text-transformer
node --check apps/api_gateway/app/static/js/model-registry.js && echo "model-registry.js OK"
grep -n "registry-add-provider" apps/api_gateway/app/static/index.html apps/api_gateway/app/static/js/model-registry.js
```
Expected: "OK"; grep shows the select in index.html and all three uses (populate, payload, updateKindFields) in the JS.

- [ ] **Step 4: Commit**

```bash
git add apps/api_gateway/app/static/index.html apps/api_gateway/app/static/js/model-registry.js
git -c user.name=lugondev -c user.email=lugondev@gmail.com commit -m "feat(admin-ui): link a Model Registry entry to a provider via add-form dropdown"
```

---

### Task 3: Browser smoke check (controller-run)

**Files:** none.

- [ ] **Step 1:** With the app running locally, log in as admin, open the Providers tab: add a provider via a preset (base_url auto-fills), confirm it lists with the key masked, edit it (blank key keeps existing), toggle enable/disable. Then in Model Registry, confirm the Provider dropdown lists the enabled provider and that picking it hides the raw base_url/api_key rows. (This is a manual/driven check; if a running instance isn't available, note it as unverified and rely on the `node --check` + grep gates.)

---

## Self-Review

- **Spec coverage:** Providers CRUD tab (T1) ✓; presets dropdown auto-fill (T1 Step 3 `_applyPreset` + `/presets`) ✓; masked key display + write-only edit (T1 rowDetail) ✓; admin-only nav (T1 Step 1 `admin-only` class) ✓; provider linkage from Model Registry add-form (T2, `config.provider_id`) ✓; base_url/api_key relaxed when linked (T2 `_updateKindFields`) ✓; static-UI verification only (node --check + grep, no pytest) ✓.
- **Placeholder scan:** all code blocks are complete; the only conditional instruction is T1 Step 1 (mirror the sibling nav-item's exact markup — the reader must read lines 106-112 first, code given for the common case).
- **Consistency:** IDs used in JS (`providers-list`, `provider-add-*`, `providers-status`, `registry-add-provider`) match the HTML added in the same/other task; `loadProviders` exported and imported in sidebar-nav; module side-effect-imported in main.js like `model-registry.js`.
