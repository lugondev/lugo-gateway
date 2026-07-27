import { el, print, escapeHtml } from "./helpers.js";

function _fmtCost(v) { return "$" + Number(v || 0).toFixed(4); }
function _fmtNum(v) { return Number(v || 0).toLocaleString(); }

export async function loadMyUsage() {
  const host = el("my-usage-list");
  if (!host) return;
  const period = (el("my-usage-period")?.value || "").trim();
  const status = el("my-usage-status");
  const qs = period ? `?period=${encodeURIComponent(period)}` : "";
  try {
    const resp = await fetch(`/v1/usage/me${qs}`);
    const body = await resp.json();
    if (!resp.ok) {
      print(status, body.detail || "Failed to load usage", true);
      host.innerHTML = "";
      return;
    }
    _render(host, body.data || [], body.limits || []);
    if (status) status.textContent = "";
  } catch (error) {
    print(status, String(error), true);
  }
}

function _renderLimits(limits) {
  if (!limits || !limits.length) return "";
  const parts = limits.map((l) => {
    const spent = Number(l.spend_usd || 0);
    const limit = Number(l.limit_usd || 0);
    const over = limit > 0 && spent >= limit;
    const label = l.scope === "global" ? "Shared limit" : "Your limit";
    return `<li class="${over ? "danger" : ""}">${label} (${escapeHtml(String(l.period))}): $${spent.toFixed(4)} of $${limit.toFixed(2)}${over ? " - reached" : ""}</li>`;
  });
  return `<ul class="limit-list">${parts.join("")}</ul>`;
}

function _render(host, rows, limits) {
  const limitsHtml = _renderLimits(limits);
  if (!rows.length) {
    host.innerHTML = `${limitsHtml}<p class="hint">No usage recorded${el("my-usage-period")?.value ? " for that month" : ""} yet.</p>`;
    return;
  }
  const sorted = [...rows].sort((a, b) => Number(b.cost_usd || 0) - Number(a.cost_usd || 0));
  const tc = sorted.reduce((s, r) => s + Number(r.cost_usd || 0), 0);
  const tn = sorted.reduce((s, r) => s + Number(r.native_amount || 0), 0);
  const tq = sorted.reduce((s, r) => s + Number(r.count || 0), 0);
  const tableHtml = `
    <table class="data-table">
      <thead>
        <tr><th>Kind</th><th>Engine</th><th>Model</th><th>Cost (USD)</th><th>Native amount</th><th>Requests</th></tr>
      </thead>
      <tbody>
        ${sorted.map((r) => `
          <tr>
            <td>${escapeHtml(String(r.kind || ""))}</td>
            <td>${escapeHtml(String(r.engine || "") || "-")}</td>
            <td><code>${escapeHtml(String(r.model_id || "") || "(not recorded)")}</code></td>
            <td>${_fmtCost(r.cost_usd)}</td>
            <td>${_fmtNum(r.native_amount)}</td>
            <td>${_fmtNum(r.count)}</td>
          </tr>`).join("")}
      </tbody>
      <tfoot>
        <tr>
          <td colspan="3"><strong>Total</strong></td>
          <td><strong>${_fmtCost(tc)}</strong></td>
          <td><strong>${_fmtNum(tn)}</strong></td>
          <td><strong>${_fmtNum(tq)}</strong></td>
        </tr>
      </tfoot>
    </table>`;
  host.innerHTML = limitsHtml + tableHtml;
}

if (el("my-usage-refresh")) el("my-usage-refresh").addEventListener("click", loadMyUsage);
if (el("my-usage-period")) el("my-usage-period").addEventListener("change", loadMyUsage);
