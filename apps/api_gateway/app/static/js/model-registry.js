import { el, print, escapeHtml, runBulk, printBulkSummary } from "./helpers.js";
import { renderDataTable } from "./data-table.js";
import { confirmDialog } from "./modal.js";

export let registryData = [];

export async function loadModelRegistry() {
  try {
    const body = await (await fetch("/v1/model_registry")).json();
    registryData = body.data || [];
    renderModelRegistry();
  } catch {
    /* ignore */
  }
}

// location comes from the backend (_location): "local" runs in-process,
// "service" calls out to an external HTTP API (openai_stt/openai_tts,
// whisper_service/eventlab, OpenRouter STT, every llm -- OpenRouter/OpenAI/
// Together are all just "service"). requires_base_url is a SEPARATE axis: a
// service whose endpoint the admin must supply shows the missing-base-URL
// warning; OpenRouter is a service with a fixed endpoint (api_key only) so it
// never warns. Falls back to requires_base_url for responses predating
// `location`.
function _baseUrlBadge(e) {
  const loc = e.location || (e.requires_base_url ? "service" : "local");
  if (loc === "local") {
    return `<span class="hint" title="Runs in-process -- no network call">local</span>`;
  }
  if (e.requires_base_url && !e.base_url) {
    return `<span class="hint" style="color:#c0392b" title="This engine needs a base URL to work">service — no base URL set!</span>`;
  }
  const title = e.base_url || "Fixed remote API endpoint — api key only";
  return `<span class="hint" title="${escapeHtml(title)}">service</span>`;
}

// artifact_installed comes from the backend (is_artifact_installed()):
// true/false for local artifact-backed engines (whisper, vosk, omnivoice,
// vieneu) whose model has (or hasn't) actually been downloaded via the
// Models page, null/None for everything else (service engines, sentinel
// rows, package-only engines) where the concept doesn't apply -- only the
// explicit `false` case gets a warning, not null.
function _artifactBadge(e) {
  if (e.artifact_installed !== false) return "";
  return `<span class="hint" style="color:#c0392b" title="Enabling this will be rejected until it's downloaded">not installed!</span>`;
}

function _filteredRegistryData() {
  const kind = el("registry-filter-kind")?.value || "";
  const stage = el("registry-filter-stage")?.value || "";
  const search = (el("registry-filter-search")?.value || "").trim().toLowerCase();
  return registryData.filter((e) => {
    if (kind && e.kind !== kind) return false;
    if (stage && e.stage !== stage) return false;
    if (search) {
      const haystack = `${e.engine} ${e.model_id} ${e.label}`.toLowerCase();
      if (!haystack.includes(search)) return false;
    }
    return true;
  });
}

// Sentinel rows (model_id == "") are engine-config, not selectable models:
// they hold device/compute/model_path settings the providers read at runtime
// (resolve_stt_engine_config). Splitting them into their own section keeps them
// from looking like phantom "downloaded" models in the main list.
function _isEngineConfig(e) {
  return e.model_id === "";
}

function renderModelRegistry() {
  const host = el("model-registry-list");
  if (!host) return;

  const rows = _filteredRegistryData();
  const modelRows = rows.filter((e) => !_isEngineConfig(e));
  const configRows = rows.filter(_isEngineConfig);

  host.innerHTML = "";
  const modelsHost = document.createElement("div");
  host.appendChild(modelsHost);
  _renderRegistryTable(modelsHost, modelRows, registryData.length ? "No models match the current filters." : "No entries yet.");

  if (configRows.length) {
    const label = document.createElement("h3");
    label.className = "sub";
    label.textContent = "Engine config (device / compute — not selectable models)";
    host.appendChild(label);
    const configHost = document.createElement("div");
    host.appendChild(configHost);
    _renderRegistryTable(configHost, configRows, "");
  }
}

function _renderRegistryTable(host, rows, emptyMessage) {
  const table = renderDataTable({
    container: host,
    rows,
    rowKey: (e) => e.id,
    emptyMessage,
    getRowClass: (e) => (e.enabled ? "" : "dim"),
    columns: [
      { key: "kind", label: "Kind", render: (e) => `<strong>${escapeHtml(e.kind)}</strong>` },
      {
        key: "model",
        label: "Engine / Model",
        render: (e) => `
          <code>${escapeHtml(e.engine)}/${escapeHtml(e.model_id)}</code>
          ${_baseUrlBadge(e)}
          ${_artifactBadge(e)}
        `,
      },
      { key: "label", label: "Label", render: (e) => escapeHtml(e.label) },
      {
        key: "stage",
        label: "Stage",
        render: (e) => `
          <select data-registry-stage="${escapeHtml(e.id)}">
            <option value="stable" ${e.stage === "stable" ? "selected" : ""}>stable</option>
            <option value="testing" ${e.stage === "testing" ? "selected" : ""}>testing</option>
          </select>
        `,
      },
      {
        key: "actions",
        label: "",
        headerClass: "dt-actions-cell",
        cellClass: "dt-actions-cell",
        render: (e) => `
          <button class="mini" data-registry-edit="${escapeHtml(e.id)}">Edit</button>
          <button class="mini" data-registry-toggle="${escapeHtml(e.id)}">${e.enabled ? "Disable" : "Enable"}</button>
          <button class="mini danger" data-registry-delete="${escapeHtml(e.id)}" ${e.enabled ? `disabled title="Disable this entry first"` : ""}>Delete</button>
        `,
      },
    ],
    rowDetail: (e) => _detailHtml(e),
    bulkActions: [
      { label: "Enable selected", run: (ids) => bulkPatchEntries(ids, { enabled: true }, "Enabled") },
      { label: "Disable selected", run: (ids) => bulkPatchEntries(ids, { enabled: false }, "Disabled") },
      { label: "Set stage: stable", run: (ids) => bulkPatchEntries(ids, { stage: "stable" }, "Updated") },
      { label: "Set stage: testing", run: (ids) => bulkPatchEntries(ids, { stage: "testing" }, "Updated") },
    ],
  });
  if (!table) return;

  table.querySelectorAll("[data-registry-stage]").forEach((sel) =>
    sel.addEventListener("change", () =>
      patchEntry(sel.getAttribute("data-registry-stage"), { stage: sel.value })
    )
  );
  table.querySelectorAll("[data-registry-toggle]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-registry-toggle");
      const entry = registryData.find((e) => e.id === id);
      patchEntry(id, { enabled: !entry.enabled });
    })
  );
  table.querySelectorAll("[data-registry-delete]").forEach((btn) =>
    btn.addEventListener("click", () => deleteEntry(btn.getAttribute("data-registry-delete")))
  );

  const detailState = new Map(); // id -> { schema, mode }

  table.querySelectorAll("[data-registry-edit]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const id = btn.getAttribute("data-registry-edit");
      const nowOpen = table.toggleDetail(id);
      if (!nowOpen || detailState.has(id)) return;
      const entry = registryData.find((x) => x.id === id);
      const schema = await _fetchSchema(entry.kind, entry.engine);
      detailState.set(id, { schema, mode: "form" });
      _renderConfigForm(id, schema, entry.config || {});
    })
  );

  table.querySelectorAll("[data-config-mode]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const detail = btn.closest("[data-registry-detail]");
      const id = detail.getAttribute("data-registry-detail");
      const mode = btn.getAttribute("data-config-mode");
      const form = detail.querySelector("[data-config-form]");
      const raw = detail.querySelector("[data-config-raw]");
      const err = detail.querySelector("[data-config-error]");
      err.hidden = true;
      if (mode === "raw") {
        // form -> raw: serialize (only if the form has fields loaded)
        try {
          const st = detailState.get(id);
          if (st && st.schema.length) raw.value = JSON.stringify(_configFromForm(id), null, 2);
        } catch (e) { err.textContent = e.message; err.hidden = false; return; }
        form.hidden = true; raw.hidden = false;
      } else {
        // raw -> form: parse back
        try {
          const parsed = JSON.parse(raw.value || "{}");
          const st = detailState.get(id);
          _renderConfigForm(id, st.schema, parsed);
        } catch { err.textContent = "Invalid JSON — fix it or stay in Raw mode"; err.hidden = false; return; }
        raw.hidden = true; form.hidden = false;
      }
      _setModeButtons(detail, mode);
    })
  );

  table.querySelectorAll("[data-detail-save]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const id = btn.getAttribute("data-detail-save");
      const detail = _detailEl(id);
      const err = detail.querySelector("[data-config-error]");
      err.hidden = true;
      const rawVisible = !detail.querySelector("[data-config-raw]").hidden;
      let config;
      try {
        config = rawVisible
          ? JSON.parse(detail.querySelector("[data-config-raw]").value || "{}")
          : _configFromForm(id);
      } catch (e) { err.textContent = e.message || "Invalid config"; err.hidden = false; return; }

      const fields = { config };
      const apikey = detail.querySelector("[data-detail-apikey]").value.trim();
      if (apikey) fields.api_key = apikey; // blank = keep existing
      fields.base_url = detail.querySelector("[data-detail-baseurl]").value.trim();
      await patchEntry(id, fields);
    })
  );
}

function _detailHtml(e) {
  return `
    <div class="registry-detail" data-registry-detail="${escapeHtml(e.id)}">
      <label class="registry-field">
        <span>API Key</span>
        <code class="hint">${escapeHtml(e.api_key || "not set")}</code>
        <input type="password" class="mini" data-detail-apikey placeholder="new key…" autocomplete="off" />
      </label>
      <label class="registry-field">
        <span>Base URL ${e.requires_base_url ? "(required)" : (e.location === "service" ? "(not needed — fixed remote API)" : "(not needed — runs in-process)")}</span>
        <input type="text" class="mini" data-detail-baseurl value="${escapeHtml(e.base_url || "")}" placeholder="https://…" ${e.requires_base_url ? "" : "disabled"} />
      </label>
      <div class="registry-field">
        <span>Config</span>
        <div class="config-mode-toggle">
          <button type="button" class="mini" data-config-mode="form">Form</button>
          <button type="button" class="mini ghost" data-config-mode="raw">Raw JSON</button>
        </div>
        <div class="config-form" data-config-form>Loading fields…</div>
        <textarea class="mini config-raw" rows="4" data-config-raw hidden>${escapeHtml(JSON.stringify(e.config || {}, null, 2))}</textarea>
        <p class="config-error hint" data-config-error hidden></p>
      </div>
      <button class="mini" data-detail-save="${escapeHtml(e.id)}">Save</button>
    </div>`;
}

async function _fetchSchema(kind, engine) {
  try {
    const r = await fetch(`/v1/model_registry/config_schema?kind=${encodeURIComponent(kind)}&engine=${encodeURIComponent(engine)}`, { credentials: "same-origin" });
    if (!r.ok) return [];
    return (await r.json()).fields || [];
  } catch {
    return [];
  }
}

function _detailEl(id) {
  return document.querySelector(`[data-registry-detail="${CSS.escape(id)}"]`);
}

function _setModeButtons(detail, mode) {
  detail.querySelectorAll("[data-config-mode]").forEach((btn) => {
    btn.classList.toggle("ghost", btn.getAttribute("data-config-mode") !== mode);
  });
}

function _renderConfigForm(id, schema, config) {
  const host = _detailEl(id).querySelector("[data-config-form]");
  if (!schema.length) {
    host.innerHTML = `<p class="hint">No preset fields for this engine — use Raw JSON.</p>`;
    return;
  }
  host.innerHTML = schema.map((f) => {
    const val = config[f.key];
    if (f.type === "bool") {
      return `<label class="config-row"><input type="checkbox" data-cfg="${escapeHtml(f.key)}" ${val ? "checked" : ""}/> ${escapeHtml(f.key)}</label>`;
    }
    const inputType = (f.type === "int" || f.type === "float") ? "number" : "text";
    const v = val === undefined ? "" : String(val);
    return `<label class="config-row"><span>${escapeHtml(f.key)}</span>
      <input type="${inputType}" data-cfg="${escapeHtml(f.key)}" data-cfg-type="${f.type}"
             value="${escapeHtml(v)}" placeholder="${escapeHtml(String(f.default))}" /></label>`;
  }).join("");
}

// Gather the form into a typed config object. Throws on a bad number.
function _configFromForm(id) {
  const host = _detailEl(id).querySelector("[data-config-form]");
  const out = {};
  host.querySelectorAll("[data-cfg]").forEach((input) => {
    const key = input.getAttribute("data-cfg");
    if (input.type === "checkbox") { out[key] = input.checked; return; }
    const raw = input.value.trim();
    if (raw === "") return; // omit empty -> resolver falls back to default
    const t = input.getAttribute("data-cfg-type");
    if (t === "int" || t === "float") {
      const n = Number(raw);
      if (Number.isNaN(n)) throw new Error(`${key} must be a number`);
      out[key] = t === "int" ? Math.trunc(n) : n;
    } else {
      out[key] = raw;
    }
  });
  return out;
}

async function _patchEntryRaw(id, fields) {
  try {
    const resp = await fetch(`/v1/model_registry/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(fields),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      return { ok: false, error: body.detail || "Update failed" };
    }
    return { ok: true };
  } catch (error) {
    return { ok: false, error: String(error) };
  }
}

async function patchEntry(id, fields) {
  const result = await _patchEntryRaw(id, fields);
  if (!result.ok) {
    print(el("model-registry-status"), result.error, true);
    return;
  }
  await loadModelRegistry();
}

async function deleteEntry(id) {
  const entry = registryData.find((e) => e.id === id);
  if (!(await confirmDialog(`Delete "${entry?.label || id}" permanently? This cannot be undone.`, { danger: true }))) return;
  try {
    const resp = await fetch(`/v1/model_registry/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      print(el("model-registry-status"), body.detail || "Delete failed", true);
      return;
    }
    await loadModelRegistry();
  } catch (error) {
    print(el("model-registry-status"), String(error), true);
  }
}

async function bulkPatchEntries(ids, fields, verb) {
  const errors = await runBulk(
    ids,
    (id) => _patchEntryRaw(id, fields),
    (id) => registryData.find((e) => e.id === id)?.label || id
  );
  await loadModelRegistry();
  printBulkSummary(el("model-registry-status"), ids.length, errors, verb);
}

function _updateKindFields() {
  const kind = el("registry-add-kind").value;
  const isLlmOrStt = kind === "llm" || kind === "stt";
  // Base URL matters for every kind now: llm/stt point at an OpenAI-compatible
  // endpoint, and tts (e.g. openai_tts) needs one too -- apps/model_service is
  // wired in as a remote engine the same way for all three.
  el("registry-add-llm-fields").classList.toggle("hidden", !(isLlmOrStt || kind === "tts"));
  // tts still uses the plain "API Key" field below rather than the one paired
  // with Base URL above -- hide that paired input for tts so it doesn't show
  // two "API Key" inputs at once.
  el("registry-add-llm-apikey-wrap").classList.toggle("hidden", kind === "tts");
  el("registry-add-key-fields").classList.toggle("hidden", isLlmOrStt);
}

export async function createModelRegistryEntry() {
  const status = el("model-registry-status");
  const kind = el("registry-add-kind").value;
  const engine = el("registry-add-engine").value.trim();
  const modelId = el("registry-add-model-id").value.trim();
  const label = el("registry-add-label").value.trim();
  const stage = el("registry-add-stage").value;
  if (!engine || !modelId || !label) {
    print(status, "Enter engine, model id, and label", true);
    return;
  }
  const payload = { kind, engine, model_id: modelId, label, stage };
  if (kind === "llm" || kind === "stt") {
    // stt: base_url is only meaningful for remote engines (whisper_service,
    // eventlab); api_key only for OpenRouter-backed engines (qwen3_asr_or/
    // whisper_or) -- other stt engines just ignore either being empty.
    payload.base_url = el("registry-add-base-url").value.trim();
    payload.api_key = el("registry-add-api-key").value.trim();
  } else {
    // tts: base_url matters for openai_tts (the apps/model_service base URL);
    // other tts engines just ignore it being empty. api_key still comes from
    // the plain field -- no current engine reads it, stored for a future
    // key-requiring one.
    payload.base_url = el("registry-add-base-url").value.trim();
    payload.api_key = el("registry-add-key-api-key").value.trim();
  }
  status.textContent = "Testing…";
  try {
    const resp = await fetch("/v1/model_registry", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await resp.json();
    if (!resp.ok) {
      print(status, body.detail || "Test failed", true);
      return;
    }
    status.textContent = `Added "${label}"`;
    el("registry-add-engine").value = "";
    el("registry-add-model-id").value = "";
    el("registry-add-label").value = "";
    if (el("registry-add-key-api-key")) el("registry-add-key-api-key").value = "";
    await loadModelRegistry();
  } catch (error) {
    print(status, String(error), true);
  }
}

if (el("registry-add-kind")) {
  el("registry-add-kind").addEventListener("change", _updateKindFields);
  _updateKindFields();
}
if (el("registry-add-btn")) el("registry-add-btn").addEventListener("click", createModelRegistryEntry);
if (el("model-registry-refresh")) el("model-registry-refresh").addEventListener("click", loadModelRegistry);
if (el("registry-filter-kind")) el("registry-filter-kind").addEventListener("change", renderModelRegistry);
if (el("registry-filter-stage")) el("registry-filter-stage").addEventListener("change", renderModelRegistry);
if (el("registry-filter-search")) el("registry-filter-search").addEventListener("input", renderModelRegistry);
