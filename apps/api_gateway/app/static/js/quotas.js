import { el, print, escapeHtml, runBulk, printBulkSummary } from "./helpers.js";
import { renderDataTable } from "./data-table.js";
import { confirmDialog } from "./modal.js";

export let quotaData = [];

export async function loadQuotas() {
  try {
    const body = await (await fetch("/v1/quotas")).json();
    quotaData = body.data || [];
    renderQuotas();
  } catch {
    /* ignore */
  }
}

function renderQuotas() {
  const host = el("quotas-list");
  if (!host) return;
  host.innerHTML = "";
  const table = renderDataTable({
    container: host,
    rows: quotaData,
    rowKey: (q) => q.id,
    emptyMessage: "No quotas yet — add one below.",
    getRowClass: (q) => (q.enabled ? "" : "dim"),
    columns: [
      { key: "scope", label: "Scope", render: (q) => `<strong>${escapeHtml(q.scope)}</strong>` },
      { key: "scope_id", label: "Scope ID", render: (q) => `<code>${escapeHtml(q.scope_id || "—")}</code>` },
      { key: "limit_usd", label: "Limit (USD)", render: (q) => escapeHtml(String(q.limit_usd)) },
      {
        key: "spend_usd",
        label: "Spent",
        render: (q) => {
          const spent = Number(q.spend_usd || 0);
          const limit = Number(q.limit_usd || 0);
          const pct = limit > 0 ? Math.round((spent / limit) * 100) : 0;
          const over = limit > 0 && spent >= limit;
          return `<span class="${over ? "danger" : ""}">$${spent.toFixed(4)} (${pct}%)</span>`;
        },
      },
      { key: "period", label: "Period", render: (q) => escapeHtml(q.period) },
      {
        key: "actions",
        label: "",
        headerClass: "dt-actions-cell",
        cellClass: "dt-actions-cell",
        render: (q) => `
          <button class="mini" data-quota-edit="${escapeHtml(q.id)}">Edit</button>
          <button class="mini" data-quota-toggle="${escapeHtml(q.id)}">${q.enabled ? "Disable" : "Enable"}</button>
          <button class="mini danger" data-quota-delete="${escapeHtml(q.id)}">Delete</button>
        `,
      },
    ],
    rowDetail: (q) => `
      <div class="registry-detail" data-quota-detail="${escapeHtml(q.id)}">
        <label class="registry-field">
          <span>Scope ID</span>
          <input type="text" class="mini" data-detail-scopeid value="${escapeHtml(q.scope_id || "")}" placeholder="blank for global" />
        </label>
        <label class="registry-field">
          <span>Limit (USD)</span>
          <input type="number" class="mini" data-detail-limit value="${escapeHtml(String(q.limit_usd))}" step="0.01" min="0.01" />
        </label>
        <label class="registry-field">
          <span>Period</span>
          <select class="mini" data-detail-period>
            <option value="monthly" ${q.period === "monthly" ? "selected" : ""}>monthly</option>
            <option value="total" ${q.period === "total" ? "selected" : ""}>total</option>
          </select>
        </label>
        <button class="mini" data-quota-save="${escapeHtml(q.id)}">Save</button>
      </div>`,
    bulkActions: [
      { label: "Enable selected", run: (ids) => bulkPatchQuotas(ids, { enabled: true }, "Enabled") },
      { label: "Disable selected", run: (ids) => bulkPatchQuotas(ids, { enabled: false }, "Disabled") },
    ],
  });
  if (!table) return;

  table.querySelectorAll("[data-quota-toggle]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-quota-toggle");
      const q = quotaData.find((x) => x.id === id);
      patchQuota(id, { enabled: !q.enabled });
    })
  );
  table.querySelectorAll("[data-quota-delete]").forEach((btn) =>
    btn.addEventListener("click", () => deleteQuota(btn.getAttribute("data-quota-delete")))
  );
  table.querySelectorAll("[data-quota-edit]").forEach((btn) =>
    btn.addEventListener("click", () => table.toggleDetail(btn.getAttribute("data-quota-edit")))
  );
  table.querySelectorAll("[data-quota-save]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-quota-save");
      const detail = document.querySelector(`[data-quota-detail="${CSS.escape(id)}"]`);
      const fields = {
        scope_id: detail.querySelector("[data-detail-scopeid]").value.trim(),
        limit_usd: parseFloat(detail.querySelector("[data-detail-limit]").value),
        period: detail.querySelector("[data-detail-period]").value,
      };
      patchQuota(id, fields);
    })
  );
}

async function _patchQuotaRaw(id, fields) {
  try {
    const resp = await fetch(`/v1/quotas/${encodeURIComponent(id)}`, {
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

async function bulkPatchQuotas(ids, fields, verb) {
  const errors = await runBulk(
    ids,
    (id) => _patchQuotaRaw(id, fields),
    (id) => quotaData.find((q) => q.id === id)?.scope_id || id
  );
  await loadQuotas();
  printBulkSummary(el("quotas-status"), ids.length, errors, verb);
}

async function patchQuota(id, fields) {
  const result = await _patchQuotaRaw(id, fields);
  if (!result.ok) {
    print(el("quotas-status"), result.error, true);
    return;
  }
  await loadQuotas();
}

async function deleteQuota(id) {
  const q = quotaData.find((x) => x.id === id);
  if (!(await confirmDialog(`Delete quota for "${q?.scope_id || q?.scope || id}"?`, { danger: true }))) return;
  try {
    const resp = await fetch(`/v1/quotas/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      print(el("quotas-status"), body.detail || "Delete failed", true);
      return;
    }
    await loadQuotas();
  } catch (error) {
    print(el("quotas-status"), String(error), true);
  }
}

export async function createQuota() {
  const status = el("quotas-status");
  const scope = el("quota-add-scope").value;
  const scopeId = el("quota-add-scope-id").value.trim();
  const limitUsd = parseFloat(el("quota-add-limit").value);
  const period = el("quota-add-period").value;
  if (!scope) {
    print(status, "Choose a scope", true);
    return;
  }
  if (Number.isNaN(limitUsd)) {
    print(status, "Enter a limit (USD)", true);
    return;
  }
  status.textContent = "Adding…";
  try {
    const resp = await fetch("/v1/quotas", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scope, scope_id: scopeId, limit_usd: limitUsd, period, enabled: true }),
    });
    const body = await resp.json();
    if (!resp.ok) {
      print(status, body.detail || "Add failed", true);
      return;
    }
    status.textContent = "Added quota";
    el("quota-add-scope-id").value = "";
    el("quota-add-limit").value = "";
    await loadQuotas();
  } catch (error) {
    print(status, String(error), true);
  }
}

if (el("quota-add-btn")) el("quota-add-btn").addEventListener("click", createQuota);
if (el("quotas-refresh")) el("quotas-refresh").addEventListener("click", loadQuotas);
