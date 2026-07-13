import { el, print, escapeHtml, runBulk, printBulkSummary } from "./helpers.js";
import { renderDataTable } from "./data-table.js";
import { promptDialog } from "./modal.js";

export let userData = [];

export async function loadUsers() {
  try {
    const body = await (await fetch("/v1/users")).json();
    userData = body.data || [];
    renderUserList();
  } catch {
    /* ignore */
  }
}

function renderUserList() {
  const host = el("user-list");
  if (!host) return;

  const table = renderDataTable({
    container: host,
    rows: userData,
    rowKey: (u) => u.id,
    getRowClass: (u) => (u.disabled ? "dim" : ""),
    emptyMessage: "No users yet.",
    columns: [
      { key: "username", label: "Username", render: (u) => `<strong>${escapeHtml(u.username)}</strong>` },
      {
        key: "role",
        label: "Role",
        render: (u) => `
          <select data-user-role="${escapeHtml(u.id)}">
            <option value="user" ${u.role === "user" ? "selected" : ""}>user</option>
            <option value="admin" ${u.role === "admin" ? "selected" : ""}>admin</option>
          </select>
        `,
      },
      {
        key: "testing",
        label: "Testing",
        render: (u) => `<input type="checkbox" data-user-testing="${escapeHtml(u.id)}" ${u.can_use_testing ? "checked" : ""} />`,
      },
      { key: "status", label: "Status", render: (u) => (u.disabled ? "Disabled" : "Active") },
      {
        key: "actions",
        label: "",
        headerClass: "dt-actions-cell",
        cellClass: "dt-actions-cell",
        render: (u) => `
          <button class="mini" data-user-toggle-disabled="${escapeHtml(u.id)}">${u.disabled ? "Enable" : "Disable"}</button>
          <button class="mini" data-user-reset="${escapeHtml(u.id)}">Reset password</button>
        `,
      },
    ],
    bulkActions: [
      { label: "Disable selected", run: (ids) => bulkUpdateUsers(ids, { disabled: true }, "Disabled") },
      { label: "Enable selected", run: (ids) => bulkUpdateUsers(ids, { disabled: false }, "Enabled") },
      { label: "Make admin", run: (ids) => bulkUpdateUsers(ids, { role: "admin" }, "Updated") },
      { label: "Make user", run: (ids) => bulkUpdateUsers(ids, { role: "user" }, "Updated") },
    ],
  });
  if (!table) return;

  table.querySelectorAll("[data-user-role]").forEach((sel) =>
    sel.addEventListener("change", () => updateUser(sel.getAttribute("data-user-role"), { role: sel.value }))
  );
  table.querySelectorAll("[data-user-testing]").forEach((cb) =>
    cb.addEventListener("change", () =>
      updateUser(cb.getAttribute("data-user-testing"), { can_use_testing: cb.checked })
    )
  );
  table.querySelectorAll("[data-user-toggle-disabled]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-user-toggle-disabled");
      const user = userData.find((u) => u.id === id);
      updateUser(id, { disabled: !user.disabled });
    })
  );
  table.querySelectorAll("[data-user-reset]").forEach((btn) =>
    btn.addEventListener("click", () => resetUserPassword(btn.getAttribute("data-user-reset")))
  );
}

async function _patchUserRaw(id, fields) {
  try {
    const resp = await fetch(`/v1/users/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(fields),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      return { ok: false, error: body.detail || "Update failed" };
    }
    return { ok: true };
  } catch (error) {
    return { ok: false, error: String(error) };
  }
}

async function updateUser(id, fields) {
  const result = await _patchUserRaw(id, fields);
  if (!result.ok) {
    print(el("user-status"), result.error, true);
    return;
  }
  await loadUsers();
}

async function bulkUpdateUsers(ids, fields, verb) {
  const errors = await runBulk(
    ids,
    (id) => _patchUserRaw(id, fields),
    (id) => userData.find((u) => u.id === id)?.username || id
  );
  await loadUsers();
  printBulkSummary(el("user-status"), ids.length, errors, verb);
}

async function resetUserPassword(id) {
  const newPassword = await promptDialog("New password for this user:");
  if (!newPassword) return;
  try {
    const resp = await fetch(`/v1/users/${encodeURIComponent(id)}/reset_password`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ new_password: newPassword }),
    });
    if (!resp.ok) {
      const body = await resp.json();
      print(el("user-status"), body.detail || "Reset failed", true);
      return;
    }
    print(el("user-status"), "Password reset");
  } catch (error) {
    print(el("user-status"), String(error), true);
  }
}

export async function createUser() {
  const username = el("user-add-username").value.trim();
  const password = el("user-add-password").value;
  const role = el("user-add-role").value;
  const status = el("user-status");
  if (!username || !password) {
    print(status, "Enter both username and password", true);
    return;
  }
  try {
    const resp = await fetch("/v1/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password, role }),
    });
    const body = await resp.json();
    if (!resp.ok) {
      print(status, body.detail || JSON.stringify(body), true);
      return;
    }
    status.textContent = `Created "${username}"`;
    el("user-add-username").value = "";
    el("user-add-password").value = "";
    await loadUsers();
  } catch (error) {
    print(status, String(error), true);
  }
}

if (el("user-add-btn")) el("user-add-btn").addEventListener("click", createUser);
if (el("user-refresh")) el("user-refresh").addEventListener("click", loadUsers);
