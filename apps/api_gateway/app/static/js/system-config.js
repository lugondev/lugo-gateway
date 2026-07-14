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

// OpenRouter API key (used by qwen3_asr_or / whisper_or STT engines). The GET
// response returns "***" (never the real value) when a key is already stored,
// so the password field is never pre-filled — leaving it blank on save keeps
// the existing key (see PUT /v1/system/config: blank openrouter_api_key is a
// no-op, unlike base_context which is a full overwrite).
export async function loadOpenrouterKeyStatus() {
  try {
    const body = await (await fetch("/v1/system/config")).json();
    const status = el("sys-openrouter-key-status");
    status.textContent = body.data.openrouter_api_key ? "Configured (leave blank to keep)" : "Not configured";
  } catch (error) {
    print(el("sys-openrouter-key-status"), String(error), true);
  }
}

export async function saveOpenrouterKey() {
  const status = el("sys-openrouter-key-status");
  try {
    // PUT overwrites base_context unconditionally, so re-send its current
    // value here too — otherwise saving just the key would blank it out.
    const current = await (await fetch("/v1/system/config")).json();
    const resp = await fetch("/v1/system/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        base_context: current.data.base_context,
        openrouter_api_key: el("sys-openrouter-key").value,
      }),
    });
    const body = await resp.json();
    if (!resp.ok) { print(status, body.detail || JSON.stringify(body), true); return; }
    el("sys-openrouter-key").value = "";
    status.classList.remove("error");
    status.textContent = body.data.openrouter_api_key ? "Saved ✓ (configured)" : "Saved ✓ (cleared)";
  } catch (error) {
    print(status, String(error), true);
  }
}
if (el("sys-openrouter-key-save")) {
  el("sys-openrouter-key-save").addEventListener("click", saveOpenrouterKey);
  loadOpenrouterKeyStatus();
}

const GROUPS = [
  { key: "engines", label: "Engine Defaults", open: true },
  { key: "stt_local", label: "STT (Local Models)", open: false },
  { key: "omnivoice", label: "OmniVoice (TTS)", open: false },
  { key: "conversation_llm", label: "Conversation LLM", open: false },
  { key: "remote_stt", label: "Remote STT Providers", open: false },
  { key: "conversation", label: "Conversation Tuning", open: false },
  { key: "preprocessing", label: "Preprocessing (VAD/Noise)", open: false },
];

const SECRET_FIELDS = new Set([
  "conversation_llm.conversation_llm_api_key",
  "remote_stt.whisper_service_api_key",
  "remote_stt.eventlab_api_key",
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
    status.textContent = "Saved ✓ (remote STT / OmniVoice / VAD changes apply automatically, no restart needed)";
    await loadSystemConfigGroups();
  } catch (error) {
    print(status, String(error), true);
  }
}
if (el("sys-config-groups-save")) {
  el("sys-config-groups-save").addEventListener("click", saveSystemConfigGroups);
  loadSystemConfigGroups();
}

// Shared STT preprocessing config (System tab) used by batch / streaming / conversation.
export function getPreproc() {
  return {
    denoise: el("pp-denoise") ? el("pp-denoise").checked : false,
    vad: el("pp-vad") ? el("pp-vad").checked : false,
    backend: el("pp-vad-backend") ? el("pp-vad-backend").value || "energy" : "energy",
  };
}

