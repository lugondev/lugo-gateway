import { el, print, escapeHtml, runBulk, printBulkSummary } from "./helpers.js";
import { renderDataTable } from "./data-table.js";

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

function renderModelRegistry() {
  const host = el("model-registry-list");
  if (!host) return;

  const rows = _filteredRegistryData();
  const table = renderDataTable({
    container: host,
    rows,
    rowKey: (e) => e.id,
    getRowClass: (e) => (e.enabled ? "" : "dim"),
    emptyMessage: registryData.length ? "No entries match the current filters." : "No entries yet.",
    columns: [
      { key: "kind", label: "Kind", render: (e) => `<strong>${escapeHtml(e.kind)}</strong>` },
      { key: "model", label: "Engine / Model", render: (e) => `<code>${escapeHtml(e.engine)}/${escapeHtml(e.model_id)}</code>` },
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
        key: "api_key",
        label: "API Key",
        render: (e) => `<input type="password" class="mini" data-registry-apikey="${escapeHtml(e.id)}"
                 placeholder="${escapeHtml(e.api_key || "not set")}" autocomplete="off" />`,
      },
      {
        key: "base_url",
        label: "Base URL",
        render: (e) =>
          e.kind === "llm"
            ? `<input type="text" class="mini" data-registry-baseurl="${escapeHtml(e.id)}"
                 value="${escapeHtml(e.base_url || "")}" placeholder="https://…" />`
            : "—",
      },
      {
        key: "actions",
        label: "",
        headerClass: "dt-actions-cell",
        cellClass: "dt-actions-cell",
        render: (e) => `<button class="mini" data-registry-toggle="${escapeHtml(e.id)}">${e.enabled ? "Disable" : "Enable"}</button>`,
      },
    ],
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
  table.querySelectorAll("[data-registry-apikey]").forEach((input) =>
    input.addEventListener("change", () => {
      // Blank = keep the existing key (same "blank means keep" convention used
      // for every other secret field in this app); only send a PATCH when the
      // admin actually typed a new value.
      if (!input.value.trim()) return;
      patchEntry(input.getAttribute("data-registry-apikey"), { api_key: input.value.trim() });
    })
  );
  table.querySelectorAll("[data-registry-baseurl]").forEach((input) =>
    input.addEventListener("change", () =>
      patchEntry(input.getAttribute("data-registry-baseurl"), { base_url: input.value.trim() })
    )
  );
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
  el("registry-add-llm-fields").classList.toggle("hidden", kind !== "llm");
  // stt/tts share the same plain "API Key" field; llm has its own (paired with Base URL).
  el("registry-add-key-fields").classList.toggle("hidden", kind === "llm");
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
  if (kind === "llm") {
    payload.base_url = el("registry-add-base-url").value.trim();
    payload.api_key = el("registry-add-api-key").value.trim();
  } else {
    // stt: only meaningful for OpenRouter-backed engines (qwen3_asr_or/
    // whisper_or), other stt engines just ignore an empty api_key.
    // tts: no current engine reads it, stored for a future key-requiring one.
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

if (el("registry-add-kind")) el("registry-add-kind").addEventListener("change", _updateKindFields);
if (el("registry-add-btn")) el("registry-add-btn").addEventListener("click", createModelRegistryEntry);
if (el("model-registry-refresh")) el("model-registry-refresh").addEventListener("click", loadModelRegistry);
if (el("registry-filter-kind")) el("registry-filter-kind").addEventListener("change", renderModelRegistry);
if (el("registry-filter-stage")) el("registry-filter-stage").addEventListener("change", renderModelRegistry);
if (el("registry-filter-search")) el("registry-filter-search").addEventListener("input", renderModelRegistry);
