// Admin CRUD for the plugin registry (/v1/plugins).
//
// Shaped after mcp-servers.js, which is deliberate: routes/plugins.py is itself
// shaped after routes/mcp.py (same admin-gated write surface, same user-readable
// list, same masking of the secret field). Read that file before changing the
// request shapes here.
//
// The one thing that is NOT like MCP is `secret`. The gateway never calls a
// plugin -- the browser does -- so `secret` runs the other way: it is what
// authenticates the plugin's callback into POST /api/auth/introspect. Two
// consequences drive the code below:
//
//   1. GET /v1/plugins masks it to "***" for a non-admin reader.
//   2. PUT /v1/plugins/{name} is a FULL replace with no partial update, so an
//      edit must resend the secret even when the admin didn't touch it.
//
// Together those two make one specific bug possible: round-trip a masked read
// straight back into a PUT and you overwrite the real secret with the literal
// "***", silently breaking every introspect call that plugin makes. MASKED_SECRET
// and the guard in savePlugin() exist for exactly that, so don't drop them.
import { el, print, escapeHtml, runBulk, printBulkSummary } from "./helpers.js";
import { renderDataTable } from "./data-table.js";
import { confirmDialog } from "./modal.js";

const MASKED_SECRET = "***";

let pluginData = {};

export async function loadPluginsAdmin() {
  const host = el("plugin-list");
  if (!host) return;
  try {
    const resp = await fetch("/v1/plugins");
    const body = await resp.json();
    if (!resp.ok) {
      print(el("plugin-status"), body.detail || "Could not load plugins", true);
      return;
    }
    pluginData = body.data || {};
    renderPluginList();
  } catch (error) {
    print(el("plugin-status"), String(error), true);
  }
}

function mountsSummary(entry) {
  const mounts = entry.mounts || [];
  if (!mounts.length) return '<span class="hint">no mounts</span>';
  return mounts
    .map(
      (m) =>
        `<code>${escapeHtml(m.path)}</code> <span class="hint">${escapeHtml(m.kind)}${m.public ? "" : " · private"}</span>`
    )
    .join("<br />");
}

function renderPluginList() {
  const host = el("plugin-list");
  if (!host) return;
  const entries = Object.values(pluginData);

  const table = renderDataTable({
    container: host,
    rows: entries,
    rowKey: (p) => p.name,
    getRowClass: (p) => (p.enabled ? "" : "dim"),
    emptyMessage: "No plugins registered yet. Add one below.",
    columns: [
      {
        key: "enabled",
        label: "On",
        render: (p) =>
          `<input type="checkbox" data-plugin-enabled="${escapeHtml(p.name)}" ${p.enabled ? "checked" : ""} title="Enabled" />`,
      },
      { key: "name", label: "Name", render: (p) => `<strong>${escapeHtml(p.name)}</strong>` },
      {
        key: "url",
        label: "URL",
        render: (p) => `<code>${escapeHtml(p.url)}</code>`,
      },
      { key: "kind", label: "Kind", render: (p) => escapeHtml(p.kind || "feature") },
      { key: "mounts", label: "Mounts", render: mountsSummary },
      {
        key: "actions",
        label: "",
        headerClass: "dt-actions-cell",
        cellClass: "dt-actions-cell",
        render: (p) => `
          <button class="mini" data-plugin-edit="${escapeHtml(p.name)}">Edit</button>
          <button class="mini danger" data-plugin-delete="${escapeHtml(p.name)}">Delete</button>
        `,
      },
    ],
    bulkActions: [
      { label: "Enable selected", run: (ids) => bulkSetEnabled(ids, true) },
      { label: "Disable selected", run: (ids) => bulkSetEnabled(ids, false) },
      { label: "Delete selected", run: (ids) => bulkDelete(ids) },
    ],
  });
  if (!table) return;

  table.querySelectorAll("[data-plugin-enabled]").forEach((cb) =>
    cb.addEventListener("change", () =>
      toggleEnabled(cb.getAttribute("data-plugin-enabled"), cb.checked)
    )
  );
  table.querySelectorAll("[data-plugin-edit]").forEach((btn) =>
    btn.addEventListener("click", () => openEditor(btn.getAttribute("data-plugin-edit")))
  );
  table.querySelectorAll("[data-plugin-delete]").forEach((btn) =>
    btn.addEventListener("click", () => deletePlugin(btn.getAttribute("data-plugin-delete")))
  );
}

// ---- mounts editor ----

function mountRow(mount = { path: "", kind: "ws", public: true }) {
  const row = document.createElement("div");
  row.className = "row tight plugin-mount-row";
  row.innerHTML = `
    <label>
      Path
      <input type="text" class="pg-mount-path" placeholder="/live/stream" value="${escapeHtml(mount.path)}" />
    </label>
    <label>
      Kind
      <select class="pg-mount-kind">
        <option value="ws"${mount.kind === "ws" ? " selected" : ""}>ws</option>
        <option value="http"${mount.kind === "http" ? " selected" : ""}>http</option>
      </select>
    </label>
    <label class="check">
      <input type="checkbox" class="pg-mount-public" ${mount.public ? "checked" : ""} /> Public
    </label>
    <div class="actions end">
      <button type="button" class="ghost mini pg-mount-remove">Remove</button>
    </div>
  `;
  row.querySelector(".pg-mount-remove").addEventListener("click", () => row.remove());
  return row;
}

function renderMounts(mounts) {
  const host = el("pg-mounts");
  if (!host) return;
  host.innerHTML = "";
  (mounts || []).forEach((m) => host.appendChild(mountRow(m)));
}

function collectMounts() {
  const host = el("pg-mounts");
  if (!host) return [];
  return Array.from(host.querySelectorAll(".plugin-mount-row"))
    .map((row) => ({
      path: row.querySelector(".pg-mount-path").value.trim(),
      kind: row.querySelector(".pg-mount-kind").value,
      public: row.querySelector(".pg-mount-public").checked,
    }))
    .filter((m) => m.path);
}

// ---- editor panel ----

function setEditorMode(name) {
  // Empty name = create. The API keys a plugin by name and PUT can't rename
  // one, so the field is locked while editing.
  el("pg-name").disabled = !!name;
  el("plugin-panel-title").textContent = name ? `Edit "${name}"` : "New Plugin";
  el("plugin-panel").dataset.editing = name || "";
}

export function openNewPlugin() {
  el("plugin-panel").classList.remove("hidden");
  setEditorMode("");
  el("pg-name").value = "";
  el("pg-url").value = "";
  el("pg-secret").value = "";
  el("pg-kind").value = "feature";
  el("pg-enabled").checked = true;
  renderMounts([]);
  print(el("plugin-panel-status"), "");
}

async function openEditor(name) {
  const status = el("plugin-panel-status");
  try {
    const resp = await fetch(`/v1/plugins/${encodeURIComponent(name)}`);
    const body = await resp.json();
    if (!resp.ok) {
      print(el("plugin-status"), body.detail || "Could not load plugin", true);
      return;
    }
    const entry = body.data;
    el("plugin-panel").classList.remove("hidden");
    setEditorMode(name);
    el("pg-name").value = entry.name;
    el("pg-url").value = entry.url;
    el("pg-secret").value = entry.secret === MASKED_SECRET ? "" : entry.secret;
    el("pg-kind").value = entry.kind || "feature";
    el("pg-enabled").checked = !!entry.enabled;
    renderMounts(entry.mounts);
    print(
      status,
      entry.secret === MASKED_SECRET
        ? "The stored secret is hidden for your role — enter a new one to save."
        : ""
    );
  } catch (error) {
    print(el("plugin-status"), String(error), true);
  }
}

function closeEditor() {
  el("plugin-panel").classList.add("hidden");
}

export async function savePlugin() {
  const status = el("plugin-panel-status");
  const editing = el("plugin-panel").dataset.editing;
  const name = el("pg-name").value.trim();
  const url = el("pg-url").value.trim();
  const secret = el("pg-secret").value;

  if (!name || !url) {
    print(status, "Name and URL are both required.", true);
    return;
  }
  if (!secret) {
    print(status, "Enter the shared secret the plugin will authenticate with.", true);
    return;
  }
  // See the module header: writing the mask back would replace the real secret
  // with "***" and break the plugin's introspect calls.
  if (secret === MASKED_SECRET) {
    print(status, `"${MASKED_SECRET}" is the placeholder for a hidden secret, not a secret. Enter the real one.`, true);
    return;
  }

  const payload = {
    name,
    url,
    secret,
    enabled: el("pg-enabled").checked,
    kind: el("pg-kind").value,
    mounts: collectMounts(),
  };

  try {
    const resp = await fetch(
      editing ? `/v1/plugins/${encodeURIComponent(editing)}` : "/v1/plugins",
      {
        method: editing ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }
    );
    const body = await resp.json();
    if (!resp.ok) {
      print(status, body.detail || JSON.stringify(body), true);
      return;
    }
    closeEditor();
    print(el("plugin-status"), `Saved "${name}" ✓`);
    await loadPluginsAdmin();
  } catch (error) {
    print(status, String(error), true);
  }
}

// ---- row actions ----

// PUT is a full replace, so a toggle has to resend every field. The cached row
// carries the real secret for an admin reader; refuse rather than write the mask
// back (same guard as savePlugin).
async function _setEnabledRaw(name, enabled) {
  const entry = pluginData[name];
  if (!entry) return { ok: false, error: "unknown plugin" };
  if (!entry.secret || entry.secret === MASKED_SECRET) {
    return { ok: false, error: "secret is hidden for your role — edit the plugin to change it" };
  }
  try {
    const resp = await fetch(`/v1/plugins/${encodeURIComponent(name)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name,
        url: entry.url,
        secret: entry.secret,
        enabled,
        kind: entry.kind || "feature",
        mounts: entry.mounts || [],
      }),
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

async function toggleEnabled(name, enabled) {
  const result = await _setEnabledRaw(name, enabled);
  if (!result.ok) {
    print(el("plugin-status"), result.error, true);
    await loadPluginsAdmin();
    return;
  }
  if (pluginData[name]) pluginData[name].enabled = enabled;
  renderPluginList();
}

async function bulkSetEnabled(names, enabled) {
  const errors = await runBulk(names, (name) => _setEnabledRaw(name, enabled), (name) => name);
  await loadPluginsAdmin();
  printBulkSummary(el("plugin-status"), names.length, errors, enabled ? "Enabled" : "Disabled");
}

async function _deleteRaw(name) {
  try {
    const resp = await fetch(`/v1/plugins/${encodeURIComponent(name)}`, { method: "DELETE" });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      return { ok: false, error: body.detail || "Delete failed" };
    }
    return { ok: true };
  } catch (error) {
    return { ok: false, error: String(error) };
  }
}

async function deletePlugin(name) {
  if (
    !(await confirmDialog(
      `Delete plugin "${name}"? Its tab disappears for everyone and any open ticket stops resolving.`,
      { danger: true }
    ))
  ) {
    return;
  }
  const result = await _deleteRaw(name);
  if (!result.ok) {
    print(el("plugin-status"), result.error, true);
    return;
  }
  await loadPluginsAdmin();
}

async function bulkDelete(names) {
  if (!(await confirmDialog(`Delete ${names.length} plugin(s)?`, { danger: true }))) return;
  const errors = await runBulk(names, _deleteRaw, (name) => name);
  await loadPluginsAdmin();
  printBulkSummary(el("plugin-status"), names.length, errors, "Deleted");
}

if (el("plugin-refresh")) el("plugin-refresh").addEventListener("click", loadPluginsAdmin);
if (el("plugin-new-btn")) el("plugin-new-btn").addEventListener("click", openNewPlugin);
if (el("plugin-save-btn")) el("plugin-save-btn").addEventListener("click", savePlugin);
if (el("plugin-close-btn")) el("plugin-close-btn").addEventListener("click", closeEditor);
if (el("pg-mount-add")) el("pg-mount-add").addEventListener("click", () => el("pg-mounts").appendChild(mountRow()));
