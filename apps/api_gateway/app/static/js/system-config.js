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
  // Per-engine model/tuning/device settings live in Model Registry entries;
  // this group only holds engine-agnostic STT settings.
  { key: "stt_local", label: "STT (Shared Settings)", open: false },
  { key: "conversation", label: "Conversation Tuning", open: false },
  { key: "preprocessing", label: "Preprocessing (VAD/Noise)", open: false },
];

// pyannote_auth_token (the only secret field SystemConfig ever had) moved to
// an env var (PYANNOTE_AUTH_TOKEN) -- no admin-editable field is a secret
// right now, but the password-input/placeholder-masking machinery below stays
// in place for the next one.
const SECRET_FIELDS = new Set([]);

// Engine-name fields must be picked from the live engine lists, not typed
// free-text (a typo'd engine only fails at request time). kind selects which
// list to render from; optional means "" is a valid value.
const ENGINE_SELECT_FIELDS = {
  "engines.default_stt_engine": { kind: "stt" },
  "engines.default_tts_engine": { kind: "tts" },
  "conversation.conversation_fast_stt_engine": { kind: "stt", optional: true },
};

// Voice list depends on the engine chosen in the sibling select, so it is
// rendered as a shell here and (re)populated by populateVoiceOptions().
const VOICE_FIELD = "engines.default_tts_engine_voice";
const VOICE_SELECT_ID = "sys-engines-default_tts_engine_voice";
const TTS_ENGINE_SELECT_ID = "sys-engines-default_tts_engine";

// Default LLM is NOT a system_config field like default_stt_engine/
// default_tts_engine -- the conversation LLM is a single Model Registry
// kind="llm" row with is_default=true (see responder.py's _active_llm_entry),
// so this widget lives outside the generic schema-driven field loop above and
// is (re)populated by populateDefaultLlmField(), same pattern as the voice
// select. Selecting a different row PATCHes it is_default=true (and enabled=
// true, so picking a default also makes it selectable) immediately -- the
// backend enforces at most one is_default llm row, not one enabled llm row
// anymore (multiple llm rows can be enabled/selectable per-profile at once;
// see model_registry/store.py's _disable_other_llm_defaults), not bundled
// into the group Save button.
const DEFAULT_LLM_FIELD_ID = "sys-default-llm";

function fieldInputType(value) {
  if (typeof value === "boolean") return "checkbox";
  if (typeof value === "number") return "number";
  return "text";
}

function renderEngineSelect(id, current, engines, optional) {
  const options = [];
  if (optional) options.push(`<option value=""${current === "" ? " selected" : ""}>(none)</option>`);
  let hasCurrent = optional && current === "";
  for (const e of engines) {
    const selected = e.engine === current;
    if (selected) hasCurrent = true;
    // Unavailable engines stay visible but unpickable -- unless one is the
    // saved value, which must survive a round-trip through Save.
    const disabled = e.available || selected ? "" : " disabled";
    const label = e.available ? e.engine : `${e.engine} (not installed)`;
    options.push(`<option value="${e.engine}"${selected ? " selected" : ""}${disabled}>${label}</option>`);
  }
  if (!hasCurrent && current) options.unshift(`<option value="${current}" selected>${current} (unknown)</option>`);
  return `<select id="${id}">${options.join("")}</select>`;
}

async function populateVoiceOptions() {
  const voiceSel = el(VOICE_SELECT_ID);
  const engineInput = el(TTS_ENGINE_SELECT_ID);
  if (!voiceSel || !engineInput) return;
  const current = voiceSel.value;
  let voices = [];
  try {
    const body = await (await fetch(`/v1/tts/voices?engine=${encodeURIComponent(engineInput.value)}`)).json();
    voices = body.data?.voices || [];
  } catch (error) {
    /* voices optional */
  }
  voiceSel.innerHTML = '<option value="">(auto)</option>';
  voices.forEach((v) => {
    const opt = document.createElement("option");
    opt.value = v.voice;
    opt.textContent = v.label;
    voiceSel.appendChild(opt);
  });
  if (current && !voices.some((v) => v.voice === current)) {
    const opt = document.createElement("option");
    opt.value = current;
    opt.textContent = `${current} (current)`;
    voiceSel.appendChild(opt);
  }
  voiceSel.value = current;
}

function renderGroupFields(groupKey, groupValue, engineLists) {
  return Object.entries(groupValue)
    .map(([field, value]) => {
      const id = `sys-${groupKey}-${field}`;
      const key = `${groupKey}.${field}`;
      const spec = ENGINE_SELECT_FIELDS[key];
      if (spec && engineLists[spec.kind] && engineLists[spec.kind].length) {
        return `<label class="field">${field}
        ${renderEngineSelect(id, String(value), engineLists[spec.kind], spec.optional)}
      </label>`;
      }
      if (key === VOICE_FIELD) {
        return `<label class="field">${field}
        <select id="${id}"><option value="${value}" selected>${value || "(auto)"}</option></select>
      </label>`;
      }
      const isSecret = SECRET_FIELDS.has(key);
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

async function fetchEngineList(url) {
  try {
    const body = await (await fetch(url)).json();
    return body.data || null;
  } catch (error) {
    return null; // fall back to a plain text input for engine fields
  }
}

export async function loadSystemConfigGroups() {
  const root = el("sys-config-groups");
  if (!root) return;
  const [body, stt, tts] = await Promise.all([
    fetch("/v1/system/config").then((r) => r.json()),
    fetchEngineList("/v1/stt/engines"),
    fetchEngineList("/v1/tts/engines"),
  ]);
  const engineLists = { stt: stt || [], tts: tts || [] };
  root.innerHTML = GROUPS.map(
    (g) => `<details ${g.open ? "open" : ""}>
      <summary>${g.label}</summary>
      <div class="fields">${renderGroupFields(g.key, body.data[g.key], engineLists)}
      ${g.key === "engines" ? `<label class="field">DEFAULT_LLM
        <select id="${DEFAULT_LLM_FIELD_ID}" disabled><option>loading…</option></select>
      </label>` : ""}</div>
    </details>`
  ).join("\n");
  populateVoiceOptions();
  populateDefaultLlmField();
  const engineSel = el(TTS_ENGINE_SELECT_ID);
  // innerHTML above recreated the element, so a fresh listener each load.
  if (engineSel) engineSel.addEventListener("change", populateVoiceOptions);
}

// Selecting a different row PATCHes it is_default=true (and enabled=true)
// right away (see the DEFAULT_LLM_FIELD_ID comment above) -- this select has
// no "Save" step of its own, so it must reflect committed state immediately
// after the PATCH.
async function populateDefaultLlmField() {
  const sel = el(DEFAULT_LLM_FIELD_ID);
  if (!sel) return;
  let entries = [];
  try {
    const body = await (await fetch("/v1/model_registry")).json();
    entries = (body.data || []).filter((e) => e.kind === "llm");
  } catch (error) {
    sel.innerHTML = '<option value="">(failed to load)</option>';
    return;
  }
  if (!entries.length) {
    sel.innerHTML = '<option value="">(none configured — add one in Model Registry)</option>';
    sel.disabled = true;
    return;
  }
  const current = entries.find((e) => e.is_default);
  sel.innerHTML = [
    !current ? '<option value="" selected>(no default set)</option>' : "",
    ...entries.map(
      (e) =>
        `<option value="${e.id}"${e === current ? " selected" : ""}>${e.label} — ${e.model_id}</option>`
    ),
  ].join("");
  sel.disabled = false;
  sel.onchange = async () => {
    if (!sel.value) return;
    sel.disabled = true;
    try {
      await fetch(`/v1/model_registry/${encodeURIComponent(sel.value)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_default: true, enabled: true }),
      });
    } finally {
      populateDefaultLlmField();
    }
  };
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

