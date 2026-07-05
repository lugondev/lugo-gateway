import { el } from "./helpers.js";
import { chat, setCurrentSessionId } from "./chat.js";
import { addBubble } from "./conversation.js";

export async function openSessionsPanel() {
  const panel = el("session-panel");
  const list = el("session-list");
  if (!panel || !list) return;
  panel.classList.remove("hidden");
  list.innerHTML = '<p class="hint">Loading&#8230;</p>';
  const profile = el("profile-select")?.value || "";
  const url = profile ? `/v1/sessions?profile=${encodeURIComponent(profile)}` : "/v1/sessions";
  try {
    const body = await (await fetch(url)).json();
    list.innerHTML = "";
    for (const s of body.data || []) {
      const row = document.createElement("div");
      row.className = "session-row";
      const t = document.createElement("span");
      t.className = "sess-time";
      t.textContent = (s.created_at || "").slice(0, 16).replace("T", " ");
      const p = document.createElement("span");
      p.className = "sess-preview";
      p.textContent = s.preview || "(empty)";
      row.append(t, p);
      row.addEventListener("click", () => loadSession(s.id));
      list.appendChild(row);
    }
    if (!(body.data || []).length) list.innerHTML = '<p class="hint">No sessions yet.</p>';
  } catch (e) {
    list.innerHTML = '<p class="hint">Failed to load sessions.</p>';
  }
}

export async function loadSession(id) {
  let body;
  try {
    const resp = await fetch(`/v1/sessions/${id}`);
    if (!resp.ok) {
      const errBody = await resp.json().catch(() => ({}));
      const list = el("session-list");
      if (list) list.insertAdjacentHTML("afterbegin", `<p class="hint error">Failed to load session: ${errBody.detail || errBody.error || resp.statusText}</p>`);
      return;
    }
    body = await resp.json();
  } catch (error) {
    const list = el("session-list");
    if (list) list.insertAdjacentHTML("afterbegin", `<p class="hint error">Failed to load session: ${error}</p>`);
    return;
  }
  // Only mutate chat state/DOM after the fetch has succeeded.
  setCurrentSessionId(id);
  const dlg = el("chat-dialogue");
  dlg.innerHTML = "";
  chat.history = [];
  for (const m of body.data.messages || []) {
    if (m.role !== "user" && m.role !== "assistant") continue;
    addBubble(m.role, m.content);
    chat.history.push({ role: m.role, content: m.content });
  }
  el("session-panel").classList.add("hidden");
}

if (el("session-list-btn")) el("session-list-btn").addEventListener("click", openSessionsPanel);
if (el("session-close-btn")) el("session-close-btn").addEventListener("click", () => el("session-panel").classList.add("hidden"));
if (el("session-new-btn")) {
  el("session-new-btn").addEventListener("click", () => {
    setCurrentSessionId(null);
    chat.history = [];
    const dialogue = el("chat-dialogue");
    if (dialogue) dialogue.innerHTML = "";
  });
}

