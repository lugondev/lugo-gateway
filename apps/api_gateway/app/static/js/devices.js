import { el, print, escapeHtml, runBulk, printBulkSummary } from "./helpers.js";
import { renderDataTable } from "./data-table.js";
import { fetchAuthStatus } from "./session.js";
import { confirmDialog } from "./modal.js";
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
  Object.keys(profileData).sort().forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = profileData[name]?.owner_id ? `${name} (mine)` : name;
    sel.appendChild(opt);
  });
  if (profileData[prev]) sel.value = prev;
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
      {
        key: "actions",
        label: "",
        headerClass: "dt-actions-cell",
        cellClass: "dt-actions-cell",
        render: (d) => `<button class="mini danger" data-device-revoke-mine="${escapeHtml(d.id)}" ${d.revoked ? "disabled" : ""}>Revoke</button>`,
      },
    ],
    bulkActions: [
      { label: "Revoke selected", run: (ids) => bulkRevokeDevices(ids, false) },
    ],
  });
  if (!table) return;

  table.querySelectorAll("[data-device-revoke-mine]").forEach((btn) =>
    btn.addEventListener("click", () => revokeMyDevice(btn.getAttribute("data-device-revoke-mine")))
  );
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
    renderAllDeviceList();
  } catch {
    /* ignore */
  }
}

function renderAllDeviceList() {
  const host = el("device-all-list");
  if (!host) return;

  const table = renderDataTable({
    container: host,
    rows: allDeviceData,
    rowKey: (d) => d.id,
    getRowClass: (d) => (d.revoked ? "dim" : ""),
    emptyMessage: "No devices paired yet.",
    columns: [
      ...deviceColumns(true),
      {
        key: "actions",
        label: "",
        headerClass: "dt-actions-cell",
        cellClass: "dt-actions-cell",
        render: (d) => `<button class="mini danger" data-device-revoke-any="${escapeHtml(d.id)}" ${d.revoked ? "disabled" : ""}>Revoke</button>`,
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

async function revokeAnyDevice(id) {
  if (!(await confirmDialog("Revoke this device? It will need to be paired again.", { danger: true }))) return;
  const result = await _revokeDeviceRaw(id, true);
  if (!result.ok) {
    print(el("device-status"), result.error, true);
    return;
  }
  await maybeLoadAllDevices();
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
