import { el } from "./helpers.js";
import { conversationState, setCurrentSessionId, currentSessionId } from "./conversation.js";
import { addBubble } from "./voice-stream.js";
import { confirmDialog } from "./modal.js";

// The profile filter the panel is currently showing. Every bulk/clear action
// is scoped to this so "what you see is what you delete".
let currentScopeProfile = "";

function scopeQuery() {
  return currentScopeProfile
    ? `?profile=${encodeURIComponent(currentScopeProfile)}`
    : "";
}

function updateToolbarState() {
  const btn = el("session-del-selected-btn");
  if (!btn) return;
  const checked = document.querySelectorAll("#session-list .sess-check:checked").length;
  btn.disabled = checked === 0;
  btn.textContent = checked ? `Delete selected (${checked})` : "Delete selected";
}

// If the deletes removed the session currently open in the conversation, reset
// to a fresh, unsaved session so the UI doesn't point at a gone row.
function resetConversationIfSessionGone(deletedIds) {
  if (currentSessionId && deletedIds.includes(currentSessionId)) {
    setCurrentSessionId(null);
    conversationState.history = [];
    const dialogue = el("conversation-dialogue");
    if (dialogue) dialogue.innerHTML = "";
  }
}

function reportSessionError(msg) {
  const list = el("session-list");
  if (list) list.insertAdjacentHTML("afterbegin", `<p class="hint error">${msg}</p>`);
}

export async function openSessionsPanel() {
  const panel = el("session-panel");
  const list = el("session-list");
  if (!panel || !list) return;
  panel.classList.remove("hidden");
  currentScopeProfile = el("profile-select")?.value || "";
  await renderSessionList();
}

async function renderSessionList() {
  const list = el("session-list");
  if (!list) return;
  list.innerHTML = '<p class="hint">Loading&#8230;</p>';
  try {
    const body = await (await fetch(`/v1/sessions${scopeQuery()}`)).json();
    list.innerHTML = "";
    for (const s of body.data || []) {
      const row = document.createElement("div");
      row.className = "session-row";

      const check = document.createElement("input");
      check.type = "checkbox";
      check.className = "sess-check";
      check.dataset.id = s.id;
      check.addEventListener("click", (e) => e.stopPropagation());
      check.addEventListener("change", updateToolbarState);

      const t = document.createElement("span");
      t.className = "sess-time";
      t.textContent = (s.created_at || "").slice(0, 16).replace("T", " ");

      const p = document.createElement("span");
      p.className = "sess-preview";
      p.textContent = s.message_count ? s.preview || "(no user message)" : "(empty)";

      const del = document.createElement("button");
      del.className = "sess-del";
      del.title = "Delete session";
      del.innerHTML = "&#10005;";
      del.addEventListener("click", (e) => {
        e.stopPropagation();
        deleteOne(s.id);
      });

      row.append(check, t, p, del);
      row.addEventListener("click", () => loadSession(s.id));
      list.appendChild(row);
    }
    if (!(body.data || []).length) list.innerHTML = '<p class="hint">No sessions yet.</p>';
    updateToolbarState();
  } catch (e) {
    list.innerHTML = '<p class="hint">Failed to load sessions.</p>';
  }
}

async function deleteOne(id) {
  try {
    const resp = await fetch(`/v1/sessions/${id}`, { method: "DELETE" });
    if (!resp.ok && resp.status !== 404) throw new Error(resp.statusText);
    resetConversationIfSessionGone([id]);
    await renderSessionList();
  } catch (e) {
    reportSessionError(`Failed to delete session: ${e}`);
  }
}

async function deleteSelected() {
  const ids = [...document.querySelectorAll("#session-list .sess-check:checked")]
    .map((cb) => cb.dataset.id);
  if (!ids.length) return;
  try {
    const resp = await fetch("/v1/sessions/bulk_delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids }),
    });
    if (!resp.ok) throw new Error(resp.statusText);
    resetConversationIfSessionGone(ids);
    await renderSessionList();
  } catch (e) {
    reportSessionError(`Failed to delete selected sessions: ${e}`);
  }
}

async function clearSessions(onlyEmpty) {
  const scopeLabel = currentScopeProfile ? `profile "${currentScopeProfile}"` : "all profiles";
  const what = onlyEmpty ? "empty sessions" : "ALL sessions";
  if (!(await confirmDialog(`Delete ${what} for ${scopeLabel}? This cannot be undone.`, { danger: true }))) return;
  const q = scopeQuery();
  const url = `/v1/sessions${q}${onlyEmpty ? (q ? "&" : "?") + "only_empty=true" : ""}`;
  try {
    const resp = await fetch(url, { method: "DELETE" });
    if (!resp.ok) throw new Error(resp.statusText);
    // A clear may have removed the open session; reset if it's no longer listed.
    const remaining = await (await fetch(`/v1/sessions${q}`)).json().catch(() => ({ data: [] }));
    const stillThere = (remaining.data || []).some((s) => s.id === currentSessionId);
    if (currentSessionId && !stillThere) resetConversationIfSessionGone([currentSessionId]);
    await renderSessionList();
  } catch (e) {
    reportSessionError(`Failed to clear sessions: ${e}`);
  }
}

// On page load there's no "current" session yet — instead of starting every
// mode against a brand-new session_id, pick up where the user left off. Only
// the explicit "New session" button (below) should force a fresh one.
export async function resumeLatestSession() {
  if (currentSessionId) return;
  try {
    const body = await (await fetch("/v1/sessions?limit=1")).json();
    const latest = (body.data || [])[0];
    if (latest) await loadSession(latest.id);
  } catch {
    /* best-effort — falls back to starting a fresh session */
  }
}

export async function loadSession(id) {
  let body;
  try {
    const resp = await fetch(`/v1/sessions/${id}`);
    if (!resp.ok) {
      const errBody = await resp.json().catch(() => ({}));
      reportSessionError(`Failed to load session: ${errBody.detail || errBody.error || resp.statusText}`);
      return;
    }
    body = await resp.json();
  } catch (error) {
    reportSessionError(`Failed to load session: ${error}`);
    return;
  }
  // Only mutate conversation state/DOM after the fetch has succeeded.
  setCurrentSessionId(id);
  const dlg = el("conversation-dialogue");
  dlg.innerHTML = "";
  conversationState.history = [];
  for (const m of body.data.messages || []) {
    if (m.role !== "user" && m.role !== "assistant") continue;
    addBubble(m.role, m.content);
    conversationState.history.push({ role: m.role, content: m.content });
  }
  el("session-panel").classList.add("hidden");
}

if (el("session-list-btn")) el("session-list-btn").addEventListener("click", openSessionsPanel);
if (el("session-close-btn")) el("session-close-btn").addEventListener("click", () => el("session-panel").classList.add("hidden"));
if (el("session-del-selected-btn")) el("session-del-selected-btn").addEventListener("click", deleteSelected);
if (el("session-clear-empty-btn")) el("session-clear-empty-btn").addEventListener("click", () => clearSessions(true));
if (el("session-clear-all-btn")) el("session-clear-all-btn").addEventListener("click", () => clearSessions(false));
if (el("session-new-btn")) {
  el("session-new-btn").addEventListener("click", () => {
    setCurrentSessionId(null);
    conversationState.history = [];
    const dialogue = el("conversation-dialogue");
    if (dialogue) dialogue.innerHTML = "";
  });
}
