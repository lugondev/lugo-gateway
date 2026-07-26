import { el, print, escapeHtml } from "./helpers.js";

// Column header for the "key" varies with the group-by dimension.
const _KEY_LABEL = {
  provider: "Provider", model: "Model", kind: "Kind", engine: "Engine", user: "User",
};

// What a BLANK key means, per dimension. These are distinct states, not one
// "unknown": no provider_id means the model runs locally with its own creds,
// and no user_id is the shared-device bucket. Only a missing model/engine is
// genuinely unrecorded (a row written before per-model attribution existed).
const _EMPTY_KEY = {
  provider: "(local - no provider)",
  user: "(shared device)",
  model: "(not recorded)",
  engine: "(not recorded)",
  kind: "(not recorded)",
};

function _fmtCost(v) {
  const n = Number(v || 0);
  // 4 dp is enough for per-period totals; show "$0.0000" rather than "$0" so a
  // priced-but-tiny total is distinguishable from a genuinely uncosted row.
  return "$" + n.toFixed(4);
}

function _fmtNum(v) {
  return Number(v || 0).toLocaleString();
}

export async function loadUsage() {
  const host = el("usage-list");
  if (!host) return;
  const groupBy = el("usage-group-by")?.value || "provider";
  const period = (el("usage-period")?.value || "").trim();
  const status = el("usage-status");
  const params = new URLSearchParams({ group_by: groupBy });
  if (period) params.set("period", period);
  try {
    const resp = await fetch(`/v1/usage/summary?${params.toString()}`);
    const body = await resp.json();
    if (!resp.ok) {
      print(status, body.detail || "Failed to load usage", true);
      host.innerHTML = "";
      return;
    }
    _render(host, body.data || [], groupBy);
    if (status) status.textContent = "";
  } catch (error) {
    print(status, String(error), true);
  }
}

// The server labels provider/user rows with a readable name (see
// usage.py:_attach_labels); show that instead of the uuid, keeping the id as a
// title so it is still copyable when debugging. Everything else is already a
// name, and a blank key is worded per dimension rather than as "unknown".
function _renderKey(row, groupBy) {
  const key = String(row.key || "");
  if (!key) return `<code>${escapeHtml(_EMPTY_KEY[groupBy] || "(not recorded)")}</code>`;
  if (row.label) {
    return `<span title="${escapeHtml(key)}">${escapeHtml(row.label)}</span>`;
  }
  return `<code>${escapeHtml(key)}</code>`;
}

function _render(host, rows, groupBy) {
  if (!rows.length) {
    host.innerHTML = `<p class="hint">No usage recorded${el("usage-period")?.value ? " for that month" : ""} yet.</p>`;
    return;
  }
  const sorted = [...rows].sort((a, b) => Number(b.cost_usd || 0) - Number(a.cost_usd || 0));
  const totalCost = sorted.reduce((s, r) => s + Number(r.cost_usd || 0), 0);
  const totalNative = sorted.reduce((s, r) => s + Number(r.native_amount || 0), 0);
  const totalCount = sorted.reduce((s, r) => s + Number(r.count || 0), 0);
  const keyLabel = _KEY_LABEL[groupBy] || "Key";
  host.innerHTML = `
    <table class="data-table">
      <thead>
        <tr>
          <th>${escapeHtml(keyLabel)}</th>
          <th>Cost (USD)</th>
          <th>Native amount</th>
          <th>Requests</th>
        </tr>
      </thead>
      <tbody>
        ${sorted.map((r) => `
          <tr>
            <td>${_renderKey(r, groupBy)}</td>
            <td>${_fmtCost(r.cost_usd)}</td>
            <td>${_fmtNum(r.native_amount)}</td>
            <td>${_fmtNum(r.count)}</td>
          </tr>`).join("")}
      </tbody>
      <tfoot>
        <tr>
          <td><strong>Total</strong></td>
          <td><strong>${_fmtCost(totalCost)}</strong></td>
          <td><strong>${_fmtNum(totalNative)}</strong></td>
          <td><strong>${_fmtNum(totalCount)}</strong></td>
        </tr>
      </tfoot>
    </table>`;
}

if (el("usage-refresh")) el("usage-refresh").addEventListener("click", loadUsage);
if (el("usage-group-by")) el("usage-group-by").addEventListener("change", loadUsage);
if (el("usage-period")) el("usage-period").addEventListener("change", loadUsage);
