import { el, print } from "./helpers.js";
import { profileData, renderProfileMcpList } from "./profiles.js";
import { fetchAuthStatus } from "./session.js";

export let mcpServerData = {};     // loaded first so profile panel can use it

export async function loadMcpServers() {
  try {
    const body = await (await fetch("/v1/mcp/servers")).json();
    mcpServerData = body.data || {};
    renderMcpList();
  } catch {
    /* ignore */
  }
}

export async function renderMcpList() {
  const host = el("mcp-server-list");
  if (!host) return;
  const servers = Object.values(mcpServerData);
  if (!servers.length) {
    host.innerHTML = '<p class="hint">No servers configured yet. Add one below.</p>';
    return;
  }
  const status = await fetchAuthStatus();
  const isAdmin = !!(status && status.authenticated && status.role === "admin");

  host.innerHTML = servers.map((s) => {
    const isTemplate = s.owner_id === null || s.owner_id === undefined;
    const mine = !isTemplate ? '<span class="hint">mine</span>' : "";
    const hideWriteControls = isTemplate && !isAdmin;
    return `
    <div class="model-row ${s.enabled ? "" : "dim"}">
      <div class="model-info">
        ${hideWriteControls ? "" : `<input type="checkbox" data-mcp-enabled="${s.name}" ${s.enabled ? "checked" : ""} title="Enabled" />`}
        <strong>${s.name}</strong>
        ${mine}
        <code>${s.url}</code>
        ${s.headers && Object.keys(s.headers).length ? `<span class="hint">headers: ${Object.keys(s.headers).join(", ")}</span>` : ""}
        <span class="mcp-row-status" id="mcp-test-${s.name}"></span>
      </div>
      <div class="model-action">
        <button class="mini" data-mcp-test="${s.name}">Test</button>
        <button class="mini" data-mcp-clone="${s.name}">Clone</button>
        ${hideWriteControls ? "" : `<button class="mini danger" data-mcp-delete="${s.name}">Delete</button>`}
      </div>
    </div>
    <div class="mcp-tool-list" id="mcp-tools-${s.name}"></div>
  `;
  }).join("");

  document.querySelectorAll("[data-mcp-enabled]").forEach((cb) =>
    cb.addEventListener("change", () =>
      toggleMcpServerEnabled(cb.getAttribute("data-mcp-enabled"), cb.checked)
    )
  );
  document.querySelectorAll("[data-mcp-test]").forEach((btn) =>
    btn.addEventListener("click", () => testMcpServer(btn.getAttribute("data-mcp-test")))
  );
  document.querySelectorAll("[data-mcp-delete]").forEach((btn) =>
    btn.addEventListener("click", () => deleteMcpServer(btn.getAttribute("data-mcp-delete")))
  );
  document.querySelectorAll("[data-mcp-clone]").forEach((btn) =>
    btn.addEventListener("click", () => cloneMcpServer(btn.getAttribute("data-mcp-clone")))
  );
}

export async function toggleMcpServerEnabled(name, enabled) {
  try {
    const resp = await fetch(`/v1/mcp/servers/${encodeURIComponent(name)}/enabled`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    if (!resp.ok) {
      const body = await resp.json();
      print(el("mcp-status"), body.detail || "Toggle failed", true);
      await loadMcpServers();
      return;
    }
    if (mcpServerData[name]) mcpServerData[name].enabled = enabled;
    renderMcpList();
  } catch (error) {
    print(el("mcp-status"), String(error), true);
    await loadMcpServers();
  }
}

export async function addMcpServer() {
  const name = el("mcp-add-name").value.trim();
  const url = el("mcp-add-url").value.trim();
  const headerName = el("mcp-add-header-name").value.trim();
  const headerValue = el("mcp-add-header-value").value.trim();
  const status = el("mcp-status");
  if (!name || !url) { print(status, "Enter both name and URL", true); return; }
  const headers = headerName && headerValue ? { [headerName]: headerValue } : {};
  try {
    const resp = await fetch("/v1/mcp/servers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, url, headers }),
    });
    const body = await resp.json();
    if (!resp.ok) { print(status, body.detail || JSON.stringify(body), true); return; }
    status.textContent = `Added "${name}" ✓`;
    el("mcp-add-name").value = "";
    el("mcp-add-url").value = "";
    el("mcp-add-header-name").value = "";
    el("mcp-add-header-value").value = "";
    await loadMcpServers();
    // Refresh profile panel MCP list if open
    if (el("profile-panel") && !el("profile-panel").classList.contains("hidden")) {
      const pName = el("pf-name").value;
      renderProfileMcpList(pName && profileData[pName] ? profileData[pName].mcp_servers : []);
    }
  } catch (error) {
    print(status, String(error), true);
  }
}

export function _escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML;
}

export async function testMcpServer(name) {
  const statusEl = el(`mcp-test-${name}`);
  const listEl = el(`mcp-tools-${name}`);
  if (statusEl) { statusEl.textContent = "testing…"; statusEl.className = "mcp-row-status"; }
  if (listEl) listEl.innerHTML = "";
  try {
    const resp = await fetch(`/v1/mcp/servers/${encodeURIComponent(name)}/tools`);
    const body = await resp.json();
    if (!resp.ok) {
      if (statusEl) { statusEl.textContent = `✗ ${body.detail || "error"}`; statusEl.className = "mcp-row-status err"; }
      return;
    }
    const tools = body.data.tools || [];
    if (statusEl) { statusEl.textContent = `✓ ${tools.length} tool${tools.length !== 1 ? "s" : ""}`; statusEl.className = "mcp-row-status ok"; }
    if (listEl) {
      listEl.innerHTML = tools.map((t) => `
        <div class="mcp-tool-item">
          <code>${_escapeHtml(t.name)}</code>
          <span>${_escapeHtml(t.description || "")}</span>
        </div>
      `).join("");
    }
  } catch (error) {
    if (statusEl) { statusEl.textContent = `✗ ${error}`; statusEl.className = "mcp-row-status err"; }
  }
}

export async function deleteMcpServer(name) {
  if (!confirm(`Delete MCP server "${name}"?`)) return;
  try {
    const resp = await fetch(`/v1/mcp/servers/${encodeURIComponent(name)}`, { method: "DELETE" });
    if (!resp.ok) { const b = await resp.json(); print(el("mcp-status"), b.detail || "Delete failed", true); return; }
    await loadMcpServers();
  } catch (error) {
    print(el("mcp-status"), String(error), true);
  }
}

export async function cloneMcpServer(name) {
  const new_name = prompt(`Clone "${name}" as:`, `${name}-copy`);
  if (!new_name || !new_name.trim()) return;
  try {
    const resp = await fetch(`/v1/mcp/servers/${encodeURIComponent(name)}/clone`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ new_name: new_name.trim() }),
    });
    const body = await resp.json();
    if (!resp.ok) { print(el("mcp-status"), body.detail || "Clone failed", true); return; }
    await loadMcpServers();
  } catch (error) {
    print(el("mcp-status"), String(error), true);
  }
}

if (el("mcp-add-btn")) el("mcp-add-btn").addEventListener("click", addMcpServer);
if (el("mcp-refresh")) el("mcp-refresh").addEventListener("click", loadMcpServers);

