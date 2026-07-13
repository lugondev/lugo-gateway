import { el, print } from "./helpers.js";

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

function _escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML;
}

function renderUserList() {
  const host = el("user-list");
  if (!host) return;
  if (!userData.length) {
    host.innerHTML = '<p class="hint">No users yet.</p>';
    return;
  }
  host.innerHTML = userData.map((u) => `
    <div class="model-row ${u.disabled ? "dim" : ""}">
      <div class="model-info">
        <strong>${_escapeHtml(u.username)}</strong>
        <select data-user-role="${u.id}">
          <option value="user" ${u.role === "user" ? "selected" : ""}>user</option>
          <option value="admin" ${u.role === "admin" ? "selected" : ""}>admin</option>
        </select>
        <label><input type="checkbox" data-user-testing="${u.id}" ${u.can_use_testing ? "checked" : ""} /> Testing</label>
        <span class="hint">${u.disabled ? "Disabled" : "Active"}</span>
      </div>
      <div class="model-action">
        <button class="mini" data-user-toggle-disabled="${u.id}">${u.disabled ? "Enable" : "Disable"}</button>
        <button class="mini" data-user-reset="${u.id}">Reset password</button>
      </div>
    </div>
  `).join("");

  document.querySelectorAll("[data-user-role]").forEach((sel) =>
    sel.addEventListener("change", () => updateUser(sel.getAttribute("data-user-role"), { role: sel.value }))
  );
  document.querySelectorAll("[data-user-testing]").forEach((cb) =>
    cb.addEventListener("change", () =>
      updateUser(cb.getAttribute("data-user-testing"), { can_use_testing: cb.checked })
    )
  );
  document.querySelectorAll("[data-user-toggle-disabled]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-user-toggle-disabled");
      const user = userData.find((u) => u.id === id);
      updateUser(id, { disabled: !user.disabled });
    })
  );
  document.querySelectorAll("[data-user-reset]").forEach((btn) =>
    btn.addEventListener("click", () => resetUserPassword(btn.getAttribute("data-user-reset")))
  );
}

async function updateUser(id, fields) {
  try {
    const resp = await fetch(`/v1/users/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(fields),
    });
    if (!resp.ok) {
      const body = await resp.json();
      print(el("user-status"), body.detail || "Update failed", true);
      return;
    }
    await loadUsers();
  } catch (error) {
    print(el("user-status"), String(error), true);
  }
}

async function resetUserPassword(id) {
  const newPassword = prompt("New password for this user:");
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
