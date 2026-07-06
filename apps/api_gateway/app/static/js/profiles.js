import { el, print } from "./helpers.js";
import { mcpServerData } from "./mcp-servers.js";
import { ttsProfileData } from "./tts-profiles.js";
import { setCurrentSessionId } from "./chat.js";

export let profileData = {};
export let profileEditMode = null; // null | "new" | "<profile-name>"

export async function loadProfiles() {
  try {
    const body = await (await fetch("/v1/profiles")).json();
    profileData = body.data || {};
    renderProfileSelect();
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
    opt.textContent = name;
    sel.appendChild(opt);
  });
  if (profileData[prev]) sel.value = prev;
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

export function openProfilePanel(mode, name) {
  profileEditMode = mode === "new" ? "new" : name;
  const panel = el("profile-panel");
  if (!panel) return;

  let selectedMcpServers = [];
  renderProfileTtsSelect();

  if (mode === "new") {
    el("pf-name").value = "";
    el("pf-name").disabled = false;
    el("pf-nickname").value = "";
    el("pf-system-prompt").value = "";
    el("pf-llm-url").value = "";
    el("pf-llm-model").value = "";
    el("pf-llm-key").value = "";
    if (el("pf-tts-profile")) el("pf-tts-profile").value = "";
    el("pf-delete-btn").classList.add("hidden");
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
    el("pf-llm-url").value = p.llm?.base_url || "";
    el("pf-llm-model").value = p.llm?.model || "";
    el("pf-llm-key").value = "";
    if (el("pf-tts-profile")) el("pf-tts-profile").value = p.tts?.profile_name || "";
    el("pf-delete-btn").classList.remove("hidden");
    selectedMcpServers = p.mcp_servers || [];
    el("pf-mem-enabled").checked = p.memory?.enabled ?? true;
    el("pf-mem-mode").value = p.memory?.mode || "all";
    loadMemories(name);
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
    llm: {
      base_url: el("pf-llm-url").value.trim(),
      api_key: el("pf-llm-key").value,
      model: el("pf-llm-model").value.trim(),
    },
    system_prompt: el("pf-system-prompt").value,
    tts: {
      profile_name: el("pf-tts-profile")?.value || "",
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
    if (isNew) { profileEditMode = name; el("pf-name").disabled = true; el("pf-delete-btn").classList.remove("hidden"); }
  } catch (error) {
    print(el("pf-status"), String(error), true);
  }
}

export async function deleteProfile() {
  const name = el("pf-name").value.trim();
  if (!name || !confirm(`Delete profile "${name}"?`)) return;
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
    const next = prompt("Edit memory:", m.content);
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
if (el("profile-close-btn")) el("profile-close-btn").addEventListener("click", closeProfilePanel);
if (el("pf-cancel-btn")) el("pf-cancel-btn").addEventListener("click", closeProfilePanel);
if (el("pf-save-btn")) el("pf-save-btn").addEventListener("click", saveProfile);
if (el("pf-delete-btn")) el("pf-delete-btn").addEventListener("click", deleteProfile);
if (el("profile-select")) {
  el("profile-select").addEventListener("change", () => {
    setCurrentSessionId(null);
    const dialogue = el("chat-dialogue");
    if (dialogue) dialogue.innerHTML = "";
  });
}

