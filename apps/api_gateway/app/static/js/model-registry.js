import { el, print } from "./helpers.js";

export let registryData = [];

function _escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML;
}

export async function loadModelRegistry() {
  try {
    const body = await (await fetch("/v1/model_registry")).json();
    registryData = body.data || [];
    renderModelRegistry();
  } catch {
    /* ignore */
  }
}

function renderModelRegistry() {
  const host = el("model-registry-list");
  if (!host) return;
  if (!registryData.length) {
    host.innerHTML = '<p class="hint">No entries yet.</p>';
    return;
  }
  host.innerHTML = registryData.map((e) => `
    <div class="model-row ${e.enabled ? "" : "dim"}">
      <div class="model-info">
        <strong>${_escapeHtml(e.kind)}</strong>
        <code>${_escapeHtml(e.engine)}/${_escapeHtml(e.model_id)}</code>
        <span class="hint">${_escapeHtml(e.label)}</span>
        <select data-registry-stage="${e.id}">
          <option value="stable" ${e.stage === "stable" ? "selected" : ""}>stable</option>
          <option value="testing" ${e.stage === "testing" ? "selected" : ""}>testing</option>
        </select>
      </div>
      <div class="model-action">
        <button class="mini" data-registry-toggle="${e.id}">${e.enabled ? "Disable" : "Enable"}</button>
      </div>
    </div>
  `).join("");

  document.querySelectorAll("[data-registry-stage]").forEach((sel) =>
    sel.addEventListener("change", () =>
      patchEntry(sel.getAttribute("data-registry-stage"), { stage: sel.value })
    )
  );
  document.querySelectorAll("[data-registry-toggle]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-registry-toggle");
      const entry = registryData.find((e) => e.id === id);
      patchEntry(id, { enabled: !entry.enabled });
    })
  );
}

async function patchEntry(id, fields) {
  try {
    const resp = await fetch(`/v1/model_registry/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(fields),
    });
    if (!resp.ok) {
      const body = await resp.json();
      print(el("model-registry-status"), body.detail || "Update failed", true);
      return;
    }
    await loadModelRegistry();
  } catch (error) {
    print(el("model-registry-status"), String(error), true);
  }
}

function _updateKindFields() {
  const kind = el("registry-add-kind").value;
  el("registry-add-llm-fields").classList.toggle("hidden", kind !== "llm");
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
    await loadModelRegistry();
  } catch (error) {
    print(status, String(error), true);
  }
}

if (el("registry-add-kind")) el("registry-add-kind").addEventListener("change", _updateKindFields);
if (el("registry-add-btn")) el("registry-add-btn").addEventListener("click", createModelRegistryEntry);
if (el("model-registry-refresh")) el("model-registry-refresh").addEventListener("click", loadModelRegistry);
