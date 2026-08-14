import { el, print, escapeHtml, runBulk, printBulkSummary } from "./helpers.js";
import { renderDataTable } from "./data-table.js";
import { fetchAuthStatus } from "./session.js";
import { confirmDialog, promptDialog } from "./modal.js";
import { profileData } from "./profiles.js";

export let myDeviceData = [];
export let allDeviceData = [];

function deviceColumns(includeOwner) {
  const columns = [
    { key: "name", label: "Name", render: (d) => `<strong>${escapeHtml(d.name)}</strong>` },
  ];
  if (includeOwner) {
    columns.push({ key: "owner", label: "Owner", render: (d) => escapeHtml(d.owner_username) });
  }
  columns.push(
    { key: "serial", label: "Serial", render: (d) => `<code>${escapeHtml(d.serial)}</code>` },
    { key: "last_seen", label: "Last seen", render: (d) => escapeHtml(d.last_seen_at || "never connected") },
  );
  return columns;
}

export function renderDevicePairProfileSelect() {
  const sel = el("device-pair-profile");
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = '<option value="">Select a profile&#8230;</option>';
  // Shared templates are clone-only, so the bind endpoint 400s on them --
  // offering the name would only produce an error.
  Object.keys(profileData).sort().filter((n) => !profileData[n]?.shared).forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    sel.appendChild(opt);
  });
  if (profileData[prev]) sel.value = prev;
}

export function renderAllDeviceFilterProfileOptions() {
  const sel = el("device-all-filter-profile");
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = '<option value="">All</option><option value="__unassigned__">Unassigned</option>';
  Object.keys(profileData).sort().forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = profileData[name]?.owner_id ? `${name} (mine)` : name;
    sel.appendChild(opt);
  });
  if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
}

export async function loadMyDevices() {
  renderDevicePairProfileSelect();
  try {
    const body = await (await fetch("/v1/devices/mine")).json();
    myDeviceData = body.data || [];
    renderMyDeviceList();
  } catch {
    /* ignore */
  }
  await maybeLoadAllDevices();
}

function myDeviceProfileColumn() {
  return {
    key: "profile",
    label: "Profile",
    render: (d) => {
      const options = ['<option value="">Unassigned</option>']
        .concat(
          Object.keys(profileData).sort()
            .filter((name) => !profileData[name]?.shared)
            .map((name) => {
              const label = escapeHtml(name);
              const selected = d.profile_id === name ? " selected" : "";
              return `<option value="${escapeHtml(name)}"${selected}>${label}</option>`;
            })
        )
        .join("");
      return `<select data-device-profile-select="${escapeHtml(d.id)}">${options}</select>`;
    },
  };
}

function renderMyDeviceList() {
  const host = el("device-mine-list");
  if (!host) return;

  const table = renderDataTable({
    container: host,
    rows: myDeviceData,
    rowKey: (d) => d.id,
    getRowClass: (d) => (d.revoked ? "dim" : ""),
    emptyMessage: "No devices paired yet.",
    columns: [
      ...deviceColumns(false),
      myDeviceProfileColumn(),
      {
        key: "actions",
        label: "",
        headerClass: "dt-actions-cell",
        cellClass: "dt-actions-cell",
        render: (d) => `
          <button class="mini" data-device-rename="${escapeHtml(d.id)}" ${d.revoked ? "disabled" : ""}>Rename</button>
          <button class="mini danger" data-device-revoke-mine="${escapeHtml(d.id)}" ${d.revoked ? "disabled" : ""}>Revoke</button>
        `,
      },
    ],
    bulkActions: [
      { label: "Revoke selected", run: (ids) => bulkRevokeDevices(ids, false) },
    ],
  });
  if (!table) return;

  table.querySelectorAll("[data-device-rename]").forEach((btn) =>
    btn.addEventListener("click", () => renameMyDevice(btn.getAttribute("data-device-rename")))
  );
  table.querySelectorAll("[data-device-revoke-mine]").forEach((btn) =>
    btn.addEventListener("click", () => revokeMyDevice(btn.getAttribute("data-device-revoke-mine")))
  );
  table.querySelectorAll("[data-device-profile-select]").forEach((sel) =>
    sel.addEventListener("change", () =>
      setMyDeviceProfile(sel.getAttribute("data-device-profile-select"), sel.value)
    )
  );
}

function allDeviceProfileColumn() {
  return {
    key: "profile",
    label: "Profile",
    cellClass: "dt-truncate",
    render: (d) =>
      d.profile_id
        ? `<span title="${escapeHtml(d.profile_id)}">${escapeHtml(d.profile_id)}</span>`
        : '<span class="hint">Unassigned</span>',
  };
}

async function maybeLoadAllDevices() {
  const status = await fetchAuthStatus();
  const section = el("device-all-section");
  if (!(status.authenticated && status.role === "admin")) {
    if (section) section.classList.add("hidden");
    return;
  }
  if (section) section.classList.remove("hidden");
  try {
    const body = await (await fetch("/v1/devices")).json();
    allDeviceData = body.data || [];
    renderAllDeviceFilterProfileOptions();
    renderAllDeviceList();
  } catch {
    /* ignore */
  }
}

function _filteredAllDeviceData() {
  const profile = el("device-all-filter-profile")?.value || "";
  const status = el("device-all-filter-status")?.value || "";
  const search = (el("device-all-filter-search")?.value || "").trim().toLowerCase();
  return allDeviceData.filter((d) => {
    if (profile === "__unassigned__" && d.profile_id) return false;
    if (profile && profile !== "__unassigned__" && d.profile_id !== profile) return false;
    if (status === "active" && d.revoked) return false;
    if (status === "revoked" && !d.revoked) return false;
    if (search) {
      const haystack = `${d.name} ${d.serial} ${d.owner_username}`.toLowerCase();
      if (!haystack.includes(search)) return false;
    }
    return true;
  });
}

function renderAllDeviceList() {
  const host = el("device-all-list");
  if (!host) return;

  const rows = _filteredAllDeviceData();
  const table = renderDataTable({
    container: host,
    rows,
    rowKey: (d) => d.id,
    getRowClass: (d) => (d.revoked ? "dim" : ""),
    emptyMessage: allDeviceData.length ? "No devices match the current filters." : "No devices paired yet.",
    columns: [
      ...deviceColumns(true),
      allDeviceProfileColumn(),
      {
        key: "actions",
        label: "",
        headerClass: "dt-actions-cell",
        cellClass: "dt-actions-cell",
        render: (d) => `
          <button class="mini danger" data-device-revoke-any="${escapeHtml(d.id)}" ${d.revoked ? "disabled" : ""}>Revoke</button>
          <button class="mini danger" data-device-delete="${escapeHtml(d.id)}" ${!d.revoked ? "disabled" : ""}>Delete</button>
        `,
      },
    ],
    bulkActions: [
      { label: "Revoke selected", run: (ids) => bulkRevokeDevices(ids, true) },
    ],
  });
  if (!table) return;

  table.querySelectorAll("[data-device-revoke-any]").forEach((btn) =>
    btn.addEventListener("click", () => revokeAnyDevice(btn.getAttribute("data-device-revoke-any")))
  );
  table.querySelectorAll("[data-device-delete]").forEach((btn) =>
    btn.addEventListener("click", () => deleteAnyDevice(btn.getAttribute("data-device-delete")))
  );
}

async function _revokeDeviceRaw(id, isAdminScope) {
  const path = isAdminScope
    ? `/v1/devices/${encodeURIComponent(id)}/revoke`
    : `/v1/devices/mine/${encodeURIComponent(id)}/revoke`;
  try {
    const resp = await fetch(path, { method: "POST" });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      return { ok: false, error: body.detail || "Revoke failed" };
    }
    return { ok: true };
  } catch (error) {
    return { ok: false, error: String(error) };
  }
}

async function revokeMyDevice(id) {
  if (!(await confirmDialog("Revoke this device? It will need to be paired again.", { danger: true }))) return;
  const result = await _revokeDeviceRaw(id, false);
  if (!result.ok) {
    print(el("device-status"), result.error, true);
    return;
  }
  await loadMyDevices();
}

async function setMyDeviceProfile(id, profileId) {
  if (profileId === "") {
    const ok = await confirmDialog(
      "Unassign this device from its profile? It will stop connecting until reassigned.",
      { danger: true }
    );
    if (!ok) {
      renderMyDeviceList();
      return;
    }
  }
  try {
    const resp = await fetch(`/v1/devices/mine/${encodeURIComponent(id)}/profile`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile_id: profileId }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      print(el("device-status"), body.detail || "Failed to update profile", true);
      return;
    }
    print(el("device-status"), "Profile updated");
    await loadMyDevices();
  } catch (error) {
    print(el("device-status"), String(error), true);
  }
}

// Only the owner-scoped route exists: renaming is "what I call my device", not an
// admin action, so there is no /v1/devices/{id}/name counterpart to offer in the
// all-devices table. The 128-char cap mirrors the Device.name column -- the server
// rejects longer, this just says so before the round-trip.
async function renameMyDevice(id) {
  const current = myDeviceData.find((d) => d.id === id)?.name || "";
  const name = await promptDialog("New name for this device:", current);
  if (name === null) return;
  const trimmed = name.trim();
  if (!trimmed) {
    print(el("device-status"), "Device name cannot be empty", true);
    return;
  }
  if (trimmed.length > 128) {
    print(el("device-status"), "Device name is too long (max 128 characters)", true);
    return;
  }
  try {
    const resp = await fetch(`/v1/devices/mine/${encodeURIComponent(id)}/name`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: trimmed }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      print(el("device-status"), body.detail || "Rename failed", true);
      return;
    }
    print(el("device-status"), "Device renamed");
    await loadMyDevices();
  } catch (error) {
    print(el("device-status"), String(error), true);
  }
}

async function revokeAnyDevice(id) {
  if (!(await confirmDialog("Revoke this device? It will need to be paired again.", { danger: true }))) return;
  const result = await _revokeDeviceRaw(id, true);
  if (!result.ok) {
    print(el("device-status"), result.error, true);
    return;
  }
  await maybeLoadAllDevices();
}

async function deleteAnyDevice(id) {
  if (!(await confirmDialog("Permanently delete this device? This cannot be undone.", { danger: true }))) return;
  try {
    const resp = await fetch(`/v1/devices/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      print(el("device-status"), body.detail || "Delete failed", true);
      return;
    }
    await maybeLoadAllDevices();
  } catch (error) {
    print(el("device-status"), String(error), true);
  }
}

async function bulkRevokeDevices(ids, isAdminScope) {
  if (!(await confirmDialog(`Revoke ${ids.length} device(s)? They will need to be paired again.`, { danger: true }))) return;
  const data = isAdminScope ? allDeviceData : myDeviceData;
  const errors = await runBulk(
    ids,
    (id) => _revokeDeviceRaw(id, isAdminScope),
    (id) => data.find((d) => d.id === id)?.name || id
  );
  if (isAdminScope) await maybeLoadAllDevices();
  else await loadMyDevices();
  printBulkSummary(el("device-status"), ids.length, errors, "Revoked");
}

export async function claimDevice() {
  const status = el("device-status");
  const name = el("device-pair-name").value.trim();
  const code = el("device-pair-code").value.trim();
  const profileId = el("device-pair-profile").value;
  if (!name || !code) {
    print(status, "Enter both the code shown on the device and a name for it", true);
    return;
  }
  if (!profileId) {
    print(status, "Choose a profile for this device to run", true);
    return;
  }
  try {
    const resp = await fetch("/v1/devices/pair/claim", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code, name, profile_id: profileId }),
    });
    const body = await resp.json();
    if (!resp.ok) {
      print(status, body.detail || "Pairing failed", true);
      return;
    }
    status.textContent = `Paired "${name}"`;
    el("device-pair-name").value = "";
    el("device-pair-code").value = "";
    el("device-pair-profile").value = "";
    await loadMyDevices();
  } catch (error) {
    print(status, String(error), true);
  }
}

if (el("device-pair-btn")) el("device-pair-btn").addEventListener("click", claimDevice);
if (el("device-refresh")) el("device-refresh").addEventListener("click", loadMyDevices);
if (el("device-all-filter-profile")) el("device-all-filter-profile").addEventListener("change", renderAllDeviceList);
if (el("device-all-filter-status")) el("device-all-filter-status").addEventListener("change", renderAllDeviceList);
if (el("device-all-filter-search")) el("device-all-filter-search").addEventListener("input", renderAllDeviceList);
