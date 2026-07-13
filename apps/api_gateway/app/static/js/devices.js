import { el, print } from "./helpers.js";
import { fetchAuthStatus } from "./session.js";

export let myDeviceData = [];
export let allDeviceData = [];

function _escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML;
}

function _deviceRow(d, ownerLabel, revokeAttr) {
  return `
    <div class="model-row ${d.revoked ? "dim" : ""}">
      <div class="model-info">
        <strong>${_escapeHtml(d.name)}</strong>
        ${ownerLabel}
        <code>${_escapeHtml(d.serial)}</code>
        <span class="hint">${d.last_seen_at ? "last seen " + _escapeHtml(d.last_seen_at) : "never connected"}</span>
      </div>
      <div class="model-action">
        <button class="mini danger" ${revokeAttr}="${d.id}" ${d.revoked ? "disabled" : ""}>Revoke</button>
      </div>
    </div>
  `;
}

export async function loadMyDevices() {
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
  if (!myDeviceData.length) {
    host.innerHTML = '<p class="hint">No devices paired yet.</p>';
    return;
  }
  host.innerHTML = myDeviceData.map((d) => _deviceRow(d, "", "data-device-revoke-mine")).join("");
  document.querySelectorAll("[data-device-revoke-mine]").forEach((btn) =>
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
  if (!allDeviceData.length) {
    host.innerHTML = '<p class="hint">No devices paired yet.</p>';
    return;
  }
  host.innerHTML = allDeviceData
    .map((d) => _deviceRow(d, `<span class="hint">owner: ${_escapeHtml(d.owner_username)}</span>`, "data-device-revoke-any"))
    .join("");
  document.querySelectorAll("[data-device-revoke-any]").forEach((btn) =>
    btn.addEventListener("click", () => revokeAnyDevice(btn.getAttribute("data-device-revoke-any")))
  );
}

async function revokeMyDevice(id) {
  if (!confirm("Revoke this device? It will need to be paired again.")) return;
  try {
    const resp = await fetch(`/v1/devices/mine/${encodeURIComponent(id)}/revoke`, { method: "POST" });
    if (!resp.ok) {
      const body = await resp.json();
      print(el("device-status"), body.detail || "Revoke failed", true);
      return;
    }
    await loadMyDevices();
  } catch (error) {
    print(el("device-status"), String(error), true);
  }
}

async function revokeAnyDevice(id) {
  if (!confirm("Revoke this device? It will need to be paired again.")) return;
  try {
    const resp = await fetch(`/v1/devices/${encodeURIComponent(id)}/revoke`, { method: "POST" });
    if (!resp.ok) {
      const body = await resp.json();
      print(el("device-status"), body.detail || "Revoke failed", true);
      return;
    }
    await maybeLoadAllDevices();
  } catch (error) {
    print(el("device-status"), String(error), true);
  }
}

export async function claimDevice() {
  const status = el("device-status");
  const name = el("device-pair-name").value.trim();
  const code = el("device-pair-code").value.trim();
  if (!name || !code) {
    print(status, "Enter both the code shown on the device and a name for it", true);
    return;
  }
  try {
    const resp = await fetch("/v1/devices/pair/claim", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code, name }),
    });
    const body = await resp.json();
    if (!resp.ok) {
      print(status, body.detail || "Pairing failed", true);
      return;
    }
    status.textContent = `Paired "${name}"`;
    el("device-pair-name").value = "";
    el("device-pair-code").value = "";
    await loadMyDevices();
  } catch (error) {
    print(status, String(error), true);
  }
}

if (el("device-pair-btn")) el("device-pair-btn").addEventListener("click", claimDevice);
if (el("device-refresh")) el("device-refresh").addEventListener("click", loadMyDevices);
