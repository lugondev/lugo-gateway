import { el, print, escapeHtml, runBulk, printBulkSummary } from "./helpers.js";
import { renderDataTable } from "./data-table.js";
import { profileData, renderProfileMcpList } from "./profiles.js";
import { fetchAuthStatus } from "./session.js";
import { confirmDialog, promptDialog } from "./modal.js";

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

  const table = renderDataTable({
    container: host,
    rows: servers,
    rowKey: (s) => s.name,
    getRowClass: (s) => (s.enabled ? "" : "dim"),
    emptyMessage: "No servers configured yet. Add one below.",
    columns: [
      {
        key: "enabled",
        label: "On",
        render: (s) => {
          const isTemplate = s.owner_id === null || s.owner_id === undefined;
          const hideWriteControls = isTemplate && !isAdmin;
          return hideWriteControls
            ? ""
            : `<input type="checkbox" data-mcp-enabled="${escapeHtml(s.name)}" ${s.enabled ? "checked" : ""} title="Enabled" />`;
        },
      },
      {
        key: "name",
        label: "Name",
        render: (s) => {
          const isTemplate = s.owner_id === null || s.owner_id === undefined;
          return `<strong>${escapeHtml(s.name)}</strong>${isTemplate ? "" : ' <span class="hint">mine</span>'}`;
        },
      },
      {
        key: "url",
        label: "URL",
        render: (s) => `
          <code>${escapeHtml(s.url)}</code>
          ${s.headers && Object.keys(s.headers).length ? `<br /><span class="hint">headers: ${escapeHtml(Object.keys(s.headers).join(", "))}</span>` : ""}
        `,
      },
      {
        key: "tools",
        label: "Tools",
        render: (s) => `
          <span class="mcp-row-status" id="mcp-test-${escapeHtml(s.name)}"></span>
          <div class="mcp-tool-list" id="mcp-tools-${escapeHtml(s.name)}"></div>
        `,
      },
      {
        key: "actions",
        label: "",
        headerClass: "dt-actions-cell",
        cellClass: "dt-actions-cell",
        render: (s) => {
          const isTemplate = s.owner_id === null || s.owner_id === undefined;
          const hideWriteControls = isTemplate && !isAdmin;
          return `
            <button class="mini" data-mcp-test="${escapeHtml(s.name)}">Test</button>
            <button class="mini" data-mcp-clone="${escapeHtml(s.name)}">Clone</button>
            ${hideWriteControls ? "" : `<button class="mini danger" data-mcp-delete="${escapeHtml(s.name)}">Delete</button>`}
          `;
        },
      },
    ],
    bulkActions: [
      { label: "Enable selected", run: (ids) => bulkSetMcpEnabled(ids, true) },
      { label: "Disable selected", run: (ids) => bulkSetMcpEnabled(ids, false) },
      { label: "Delete selected", run: (ids) => bulkDeleteMcpServers(ids) },
    ],
  });
  if (!table) return;

  table.querySelectorAll("[data-mcp-enabled]").forEach((cb) =>
    cb.addEventListener("change", () =>
      toggleMcpServerEnabled(cb.getAttribute("data-mcp-enabled"), cb.checked)
    )
  );
  table.querySelectorAll("[data-mcp-test]").forEach((btn) =>
    btn.addEventListener("click", () => testMcpServer(btn.getAttribute("data-mcp-test")))
  );
  table.querySelectorAll("[data-mcp-delete]").forEach((btn) =>
    btn.addEventListener("click", () => deleteMcpServer(btn.getAttribute("data-mcp-delete")))
  );
  table.querySelectorAll("[data-mcp-clone]").forEach((btn) =>
    btn.addEventListener("click", () => cloneMcpServer(btn.getAttribute("data-mcp-clone")))
  );
}

async function _setMcpEnabledRaw(name, enabled) {
  try {
    const resp = await fetch(`/v1/mcp/servers/${encodeURIComponent(name)}/enabled`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      return { ok: false, error: body.detail || "Toggle failed" };
    }
    return { ok: true };
  } catch (error) {
    return { ok: false, error: String(error) };
  }
}

export async function toggleMcpServerEnabled(name, enabled) {
  const result = await _setMcpEnabledRaw(name, enabled);
  if (!result.ok) {
    print(el("mcp-status"), result.error, true);
    await loadMcpServers();
    return;
  }
  if (mcpServerData[name]) mcpServerData[name].enabled = enabled;
  renderMcpList();
}

async function bulkSetMcpEnabled(names, enabled) {
  const errors = await runBulk(names, (name) => _setMcpEnabledRaw(name, enabled), (name) => name);
  await loadMcpServers();
  printBulkSummary(el("mcp-status"), names.length, errors, enabled ? "Enabled" : "Disabled");
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
          <code>${escapeHtml(t.name)}</code>
          <span>${escapeHtml(t.description || "")}</span>
        </div>
      `).join("");
    }
  } catch (error) {
    if (statusEl) { statusEl.textContent = `✗ ${error}`; statusEl.className = "mcp-row-status err"; }
  }
}

async function _deleteMcpServerRaw(name) {
  try {
    const resp = await fetch(`/v1/mcp/servers/${encodeURIComponent(name)}`, { method: "DELETE" });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      return { ok: false, error: body.detail || "Delete failed" };
    }
    return { ok: true };
  } catch (error) {
    return { ok: false, error: String(error) };
  }
}

export async function deleteMcpServer(name) {
  if (!(await confirmDialog(`Delete MCP server "${name}"?`, { danger: true }))) return;
  const result = await _deleteMcpServerRaw(name);
  if (!result.ok) {
    print(el("mcp-status"), result.error, true);
    return;
  }
  await loadMcpServers();
}

async function bulkDeleteMcpServers(names) {
  if (!(await confirmDialog(`Delete ${names.length} MCP server(s)?`, { danger: true }))) return;
  const errors = await runBulk(names, _deleteMcpServerRaw, (name) => name);
  await loadMcpServers();
  printBulkSummary(el("mcp-status"), names.length, errors, "Deleted");
}

export async function cloneMcpServer(name) {
  const new_name = await promptDialog(`Clone "${name}" as:`, `${name}-copy`);
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
