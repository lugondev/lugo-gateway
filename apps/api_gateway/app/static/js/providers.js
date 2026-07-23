import { el, print, escapeHtml, runBulk, printBulkSummary } from "./helpers.js";
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
    bulkActions: [
      { label: "Enable selected", run: (ids) => bulkPatchProviders(ids, { enabled: true }, "Enabled") },
      { label: "Disable selected", run: (ids) => bulkPatchProviders(ids, { enabled: false }, "Disabled") },
    ],
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

async function _patchProviderRaw(id, fields) {
  try {
    const resp = await fetch(`/v1/providers/${encodeURIComponent(id)}`, {
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

async function bulkPatchProviders(ids, fields, verb) {
  const errors = await runBulk(
    ids,
    (id) => _patchProviderRaw(id, fields),
    (id) => providerData.find((p) => p.id === id)?.name || id
  );
  await loadProviders();
  printBulkSummary(el("providers-status"), ids.length, errors, verb);
}

async function patchProvider(id, fields) {
  const result = await _patchProviderRaw(id, fields);
  if (!result.ok) {
    print(el("providers-status"), result.error, true);
    return;
  }
  await loadProviders();
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
