import { el, print } from "./helpers.js";

export async function loadBaseContext() {
  try {
    const body = await (await fetch("/v1/system/config")).json();
    el("sys-base-context").value = body.data.base_context || "";
  } catch (error) {
    print(el("sys-base-context-status"), String(error), true);
  }
}

export async function saveBaseContext() {
  const status = el("sys-base-context-status");
  try {
    const resp = await fetch("/v1/system/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base_context: el("sys-base-context").value }),
    });
    const body = await resp.json();
    if (!resp.ok) { print(status, body.detail || JSON.stringify(body), true); return; }
    status.classList.remove("error");
    status.textContent = "Saved ✓";
  } catch (error) {
    print(status, String(error), true);
  }
}
if (el("sys-base-context-save")) {
  el("sys-base-context-save").addEventListener("click", saveBaseContext);
  loadBaseContext();
}

// OpenRouter no longer has a single system-wide key -- each qwen3_asr_or/
// whisper_or model added in Model Registry carries its own api_key (see
// model-registry.js), so there is no per-system key panel here anymore.

const GROUPS = [
  { key: "engines", label: "Engine Defaults", open: true },
  { key: "stt_local", label: "STT (Local Models)", open: false },
  { key: "conversation", label: "Conversation Tuning", open: false },
  { key: "preprocessing", label: "Preprocessing (VAD/Noise)", open: false },
];

const SECRET_FIELDS = new Set([
  "preprocessing.pyannote_auth_token",
]);

function fieldInputType(value) {
  if (typeof value === "boolean") return "checkbox";
  if (typeof value === "number") return "number";
  return "text";
}

function renderGroupFields(groupKey, groupValue) {
  return Object.entries(groupValue)
    .map(([field, value]) => {
      const id = `sys-${groupKey}-${field}`;
      const isSecret = SECRET_FIELDS.has(`${groupKey}.${field}`);
      const type = isSecret ? "password" : fieldInputType(value);
      const checked = type === "checkbox" && value ? "checked" : "";
      const val = type === "checkbox" ? "" : `value="${isSecret ? "" : String(value)}"`;
      const placeholder = isSecret && value ? `placeholder="${value ? "***" : ""}"` : "";
      return `<label class="field">${field}
        <input type="${type}" id="${id}" ${val} ${checked} ${placeholder} />
      </label>`;
    })
    .join("\n");
}

export async function loadSystemConfigGroups() {
  const body = await (await fetch("/v1/system/config")).json();
  const root = el("sys-config-groups");
  if (!root) return;
  root.innerHTML = GROUPS.map(
    (g) => `<details ${g.open ? "open" : ""}>
      <summary>${g.label}</summary>
      <div class="fields">${renderGroupFields(g.key, body.data[g.key])}</div>
    </details>`
  ).join("\n");
}

export async function saveSystemConfigGroups() {
  const status = el("sys-config-groups-status");
  try {
    const current = await (await fetch("/v1/system/config")).json();
    const payload = current.data;
    for (const g of GROUPS) {
      for (const field of Object.keys(payload[g.key])) {
        const input = el(`sys-${g.key}-${field}`);
        if (!input) continue;
        payload[g.key][field] =
          input.type === "checkbox"
            ? input.checked
            : input.type === "number"
              ? Number(input.value)
              : input.value;
      }
    }
    const resp = await fetch("/v1/system/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await resp.json();
    if (!resp.ok) { print(status, body.detail || JSON.stringify(body), true); return; }
    status.classList.remove("error");
    status.textContent = "Saved ✓ (VAD changes apply automatically, no restart needed)";
    await loadSystemConfigGroups();
  } catch (error) {
    print(status, String(error), true);
  }
}
if (el("sys-config-groups-save")) {
  el("sys-config-groups-save").addEventListener("click", saveSystemConfigGroups);
  loadSystemConfigGroups();
}

