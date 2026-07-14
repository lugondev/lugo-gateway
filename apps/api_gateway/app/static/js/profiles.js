import { el, print, restoreAndBind } from "./helpers.js";
import { mcpServerData } from "./mcp-servers.js";
import { ttsProfileData } from "./tts-profiles.js";
import { setCurrentSessionId } from "./chat.js";
import { fetchAuthStatus } from "./session.js";
import { confirmDialog, promptDialog } from "./modal.js";
import { resetConvManualOverrides } from "./conversation.js";
import { resetLhManualOverrides } from "./livehost.js";

export let profileData = {};
export let profileEditMode = null; // null | "new" | "<profile-name>"
export let llmOptionData = [];

export async function loadLlmOptions() {
  try {
    const body = await (await fetch("/v1/profiles/llm-options")).json();
    llmOptionData = body.data || [];
  } catch {
    llmOptionData = [];
  }
}

export async function loadProfiles() {
  try {
    const body = await (await fetch("/v1/profiles")).json();
    profileData = body.data || {};
    renderProfileSelect();
    renderLivehostProfileSelect();
  } catch {
    /* ignore */
  }
}

export function renderProfileSelect() {
  const sel = el("profile-select");
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = '<option value="">(none — server defaults)</option>';
  Object.keys(profileData).sort().forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = profileData[name]?.owner_id ? `${name} (mine)` : name;
    sel.appendChild(opt);
  });
  if (profileData[prev]) sel.value = prev;
}

export function renderLivehostProfileSelect() {
  const sel = el("lh-profile");
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = '<option value="">(none — server defaults)</option>';
  Object.keys(profileData).sort().forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = profileData[name]?.owner_id ? `${name} (mine)` : name;
    sel.appendChild(opt);
  });
  if (profileData[prev]) sel.value = prev;
  restoreAndBind("lh-profile");
}

export function renderProfileTtsSelect() {
  const sel = el("pf-tts-profile");
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = '<option value="">(inherit global)</option>';
  Object.keys(ttsProfileData).sort().forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    sel.appendChild(opt);
  });
  if (ttsProfileData[prev]) sel.value = prev;
}

export function renderProfileLlmSelect() {
  const sel = el("pf-llm-select");
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = "";
  llmOptionData.forEach((entry) => {
    const opt = document.createElement("option");
    opt.value = entry.id;
    opt.textContent = `${entry.label} (${entry.engine}/${entry.model_id})`;
    sel.appendChild(opt);
  });
  const customOpt = document.createElement("option");
  customOpt.value = "__custom__";
  customOpt.textContent = "— Custom… —";
  sel.appendChild(customOpt);
  if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
}

export function toggleLlmCustomFields() {
  const sel = el("pf-llm-select");
  const fields = el("pf-llm-custom-fields");
  if (!sel || !fields) return;
  fields.classList.toggle("hidden", sel.value !== "__custom__");
}

export async function openProfilePanel(mode, name) {
  profileEditMode = mode === "new" ? "new" : name;
  const panel = el("profile-panel");
  if (!panel) return;

  let selectedMcpServers = [];
  renderProfileTtsSelect();
  await loadLlmOptions();
  renderProfileLlmSelect();

  if (mode === "new") {
    el("pf-name").value = "";
    el("pf-name").disabled = false;
    el("pf-nickname").value = "";
    el("pf-system-prompt").value = "";
    if (el("pf-voice-optimized")) el("pf-voice-optimized").checked = false;
    el("pf-llm-select").value = "__custom__";
    el("pf-llm-url").value = "";
    el("pf-llm-model").value = "";
    el("pf-llm-key").value = "";
    toggleLlmCustomFields();
    if (el("pf-stt-profile")) el("pf-stt-profile").value = "";
    if (el("pf-tts-profile")) el("pf-tts-profile").value = "";
    if (el("pf-idle-timeout")) el("pf-idle-timeout").value = 30;
    el("pf-delete-btn").classList.add("hidden");
    if (el("pf-clone-btn")) el("pf-clone-btn").classList.add("hidden");
    el("pf-save-btn").classList.remove("hidden");
    el("pf-mem-enabled").checked = true;
    el("pf-mem-mode").value = "all";
    el("pf-mem-list").innerHTML = "";
  } else {
    const p = profileData[name];
    if (!p) return;
    el("pf-name").value = name;
    el("pf-name").disabled = true;
    el("pf-nickname").value = p.nickname || "";
    el("pf-system-prompt").value = p.system_prompt || "";
    if (el("pf-voice-optimized")) el("pf-voice-optimized").checked = p.voice_optimized ?? false;
    const matchedOption = llmOptionData.find(
      (o) => o.engine === p.llm?.engine && o.model_id === p.llm?.model
    );
    el("pf-llm-select").value = matchedOption ? matchedOption.id : "__custom__";
    el("pf-llm-url").value = p.llm?.base_url || "";
    el("pf-llm-model").value = p.llm?.model || "";
    el("pf-llm-key").value = "";
    toggleLlmCustomFields();
    if (el("pf-stt-profile")) el("pf-stt-profile").value = p.stt?.profile || "";
    if (el("pf-tts-profile")) el("pf-tts-profile").value = p.tts?.profile_name || "";
    if (el("pf-idle-timeout")) el("pf-idle-timeout").value = p.session?.idle_timeout_s ?? 30;
    selectedMcpServers = p.mcp_servers || [];
    el("pf-mem-enabled").checked = p.memory?.enabled ?? true;
    el("pf-mem-mode").value = p.memory?.mode || "all";
    loadMemories(name);

    // Templates (owner_id === null) are read-only for non-admins: the server
    // 404s Save/Delete on them anyway, so hide those controls and offer Clone only.
    const isTemplate = p.owner_id === null || p.owner_id === undefined;
    const status = await fetchAuthStatus();
    const isAdmin = !!(status && status.authenticated && status.role === "admin");
    const hideWriteControls = isTemplate && !isAdmin;
    el("pf-save-btn").classList.toggle("hidden", hideWriteControls);
    el("pf-delete-btn").classList.toggle("hidden", hideWriteControls);
    if (el("pf-clone-btn")) el("pf-clone-btn").classList.remove("hidden");
  }

  el("pf-status").textContent = "";
  panel.classList.remove("hidden");
  renderProfileMcpList(selectedMcpServers);
}

export function closeProfilePanel() {
  profileEditMode = null;
  const panel = el("profile-panel");
  if (panel) panel.classList.add("hidden");
}

export function renderProfileMcpList(selectedServers) {
  const container = el("pf-mcp-list");
  if (!container) return;
  const selectedUrls = new Set((selectedServers || []).map((s) => s.url));
  const servers = Object.values(mcpServerData);
  if (!servers.length) {
    container.innerHTML = '<p class="hint" style="margin:0">No global MCP servers. Add them in the MCP section first.</p>';
    return;
  }
  container.innerHTML = servers.map((s) => `
    <label class="pf-mcp-item">
      <input type="checkbox" data-mcp-srv="${s.name}" ${selectedUrls.has(s.url) ? "checked" : ""} />
      <span>${s.name}</span>
      <code>${s.url}</code>
    </label>
  `).join("");
}

export async function saveProfile() {
  const name = el("pf-name").value.trim();
  if (!name) { print(el("pf-status"), "Enter a profile name", true); return; }

  const selectedMcpServers = [...document.querySelectorAll("#pf-mcp-list input[data-mcp-srv]:checked")]
    .map((cb) => cb.getAttribute("data-mcp-srv"))
    .map((n) => mcpServerData[n])
    .filter(Boolean)
    .map((s) => ({ name: s.name, url: s.url }));

  // Preserve memory fields this panel doesn't expose (top_k, extractor_model, embed_model)
  // by spreading the previously loaded profile's memory config, then overriding what we edit.
  const existingMemory = profileData[name]?.memory || {};

  const payload = {
    name,
    nickname: el("pf-nickname").value.trim(),
    llm: (() => {
      const selectedId = el("pf-llm-select")?.value;
      const selected = llmOptionData.find((o) => o.id === selectedId);
      if (selected) {
        return { base_url: "", api_key: "", model: selected.model_id, engine: selected.engine };
      }
      return {
        base_url: el("pf-llm-url").value.trim(),
        api_key: el("pf-llm-key").value,
        model: el("pf-llm-model").value.trim(),
        engine: "",
      };
    })(),
    system_prompt: el("pf-system-prompt").value,
    voice_optimized: el("pf-voice-optimized")?.checked ?? false,
    stt: {
      profile: el("pf-stt-profile")?.value || "",
    },
    tts: {
      profile_name: el("pf-tts-profile")?.value || "",
    },
    session: {
      // Include session so a UI save doesn't reset idle_timeout_s to the server
      // default. Blank/invalid falls back to 30; 0 = never disconnect.
      idle_timeout_s: (() => {
        const v = parseInt(el("pf-idle-timeout")?.value, 10);
        return Number.isNaN(v) ? 30 : Math.max(0, v);
      })(),
    },
    mcp_servers: selectedMcpServers,
    memory: {
      ...existingMemory,
      enabled: el("pf-mem-enabled").checked,
      mode: el("pf-mem-mode").value,
    },
  };

  print(el("pf-status"), "Saving…");
  try {
    const isNew = profileEditMode === "new";
    const url = isNew ? "/v1/profiles" : `/v1/profiles/${encodeURIComponent(name)}`;
    const resp = await fetch(url, {
      method: isNew ? "POST" : "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await resp.json();
    if (!resp.ok) {
      print(el("pf-status"), body.detail || body.error || JSON.stringify(body), true);
      return;
    }
    el("pf-status").textContent = "Saved ✓";
    el("pf-llm-key").value = "";
    await loadProfiles();
    el("profile-select").value = name;
    if (isNew) {
      profileEditMode = name;
      el("pf-name").disabled = true;
      el("pf-delete-btn").classList.remove("hidden");
      if (el("pf-clone-btn")) el("pf-clone-btn").classList.remove("hidden");
    }
  } catch (error) {
    print(el("pf-status"), String(error), true);
  }
}

export async function deleteProfile() {
  const name = el("pf-name").value.trim();
  if (!name) return;
  if (!(await confirmDialog(`Delete profile "${name}"?`, { danger: true }))) return;
  try {
    const resp = await fetch(`/v1/profiles/${encodeURIComponent(name)}`, { method: "DELETE" });
    if (!resp.ok) { const b = await resp.json(); print(el("pf-status"), b.detail || "Delete failed", true); return; }
    await loadProfiles();
    el("profile-select").value = "";
    closeProfilePanel();
  } catch (error) {
    print(el("pf-status"), String(error), true);
  }
}

export async function cloneProfile() {
  const name = el("pf-name").value.trim();
  if (!name) return;
  const new_name = await promptDialog(`Clone "${name}" as:`, `${name}-copy`);
  if (!new_name || !new_name.trim()) return;
  try {
    const resp = await fetch(`/v1/profiles/${encodeURIComponent(name)}/clone`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ new_name: new_name.trim() }),
    });
    const body = await resp.json();
    if (!resp.ok) { print(el("pf-status"), body.detail || "Clone failed", true); return; }
    await loadProfiles();
    el("profile-select").value = new_name.trim();
    closeProfilePanel();
  } catch (error) {
    print(el("pf-status"), String(error), true);
  }
}

export async function loadMemories(name) {
  const list = el("pf-mem-list");
  if (!list) return;
  list.innerHTML = "";
  if (!name) return;
  try {
    const body = await (await fetch(`/v1/profiles/${encodeURIComponent(name)}/memories`)).json();
    for (const m of body.data || []) list.appendChild(memRow(name, m));
    if (!(body.data || []).length) list.innerHTML = '<p class="hint">No memories yet.</p>';
  } catch (e) {
    list.innerHTML = '<p class="hint">Failed to load memories.</p>';
  }
}

export function memRow(name, m) {
  const row = document.createElement("div");
  row.className = "pf-mem-item";
  const text = document.createElement("span");
  text.className = "mem-text";
  text.textContent = m.content;
  const edit = document.createElement("button");
  edit.className = "ghost mini";
  edit.type = "button";
  edit.textContent = "✎";
  edit.addEventListener("click", async () => {
    const next = await promptDialog("Edit memory:", m.content);
    if (next === null || !next.trim()) return;
    try {
      const resp = await fetch(`/v1/profiles/${encodeURIComponent(name)}/memories/${m.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: next.trim() }),
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        print(el("pf-status"), "Memory update failed: " + (body.detail || body.error || resp.statusText), true);
        return;
      }
      loadMemories(name);
    } catch (error) {
      print(el("pf-status"), "Memory update failed: " + error, true);
    }
  });
  const del = document.createElement("button");
  del.className = "ghost mini";
  del.type = "button";
  del.textContent = "✕";
  del.addEventListener("click", async () => {
    try {
      const resp = await fetch(`/v1/profiles/${encodeURIComponent(name)}/memories/${m.id}`, { method: "DELETE" });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        print(el("pf-status"), "Memory delete failed: " + (body.detail || body.error || resp.statusText), true);
        return;
      }
      loadMemories(name);
    } catch (error) {
      print(el("pf-status"), "Memory delete failed: " + error, true);
    }
  });
  row.append(text, edit, del);
  return row;
}

if (el("pf-mem-add")) {
  el("pf-mem-add").addEventListener("click", async () => {
    const name = el("pf-name").value.trim();
    const content = el("pf-mem-new").value.trim();
    if (!name || !content) return;
    try {
      const resp = await fetch(`/v1/profiles/${encodeURIComponent(name)}/memories`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content }),
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        print(el("pf-status"), "Memory add failed: " + (body.detail || body.error || resp.statusText), true);
        return;
      }
      el("pf-mem-new").value = "";
      loadMemories(name);
    } catch (error) {
      print(el("pf-status"), "Memory add failed: " + error, true);
    }
  });
}

// Profile bar event listeners
if (el("profile-edit-btn")) {
  el("profile-edit-btn").addEventListener("click", () => {
    const name = el("profile-select").value;
    if (!name) { alert("Select a profile first, or click + New to create one."); return; }
    openProfilePanel("edit", name);
  });
}
if (el("profile-new-btn")) el("profile-new-btn").addEventListener("click", () => openProfilePanel("new"));
if (el("pf-llm-select")) el("pf-llm-select").addEventListener("change", toggleLlmCustomFields);
if (el("profile-close-btn")) el("profile-close-btn").addEventListener("click", closeProfilePanel);
if (el("pf-cancel-btn")) el("pf-cancel-btn").addEventListener("click", closeProfilePanel);
if (el("pf-save-btn")) el("pf-save-btn").addEventListener("click", saveProfile);
if (el("pf-delete-btn")) el("pf-delete-btn").addEventListener("click", deleteProfile);
if (el("pf-clone-btn")) el("pf-clone-btn").addEventListener("click", cloneProfile);
if (el("profile-select")) {
  el("profile-select").addEventListener("change", () => {
    setCurrentSessionId(null);
    const dialogue = el("chat-dialogue");
    if (dialogue) dialogue.innerHTML = "";
    resetConvManualOverrides();
  });
}
if (el("lh-profile")) {
  el("lh-profile").addEventListener("change", () => resetLhManualOverrides());
}

