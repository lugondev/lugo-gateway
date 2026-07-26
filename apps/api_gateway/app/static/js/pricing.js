import { el, print, escapeHtml } from "./helpers.js";

// Rate inputs per unit, in the order the server's price_schema normalizes them.
const RATE_KEYS = {
  "1M_tokens": [
    { key: "in", label: "in / 1M" },
    { key: "out", label: "out / 1M" },
  ],
  minute: [{ key: "rate", label: "$ / minute" }],
  "1k_chars": [{ key: "rate", label: "$ / 1k chars" }],
};

let pricingRows = [];

export async function loadPricing() {
  const host = el("pricing-list");
  if (!host) return;
  try {
    const resp = await fetch("/v1/model_registry/prices");
    const body = await resp.json();
    if (!resp.ok) {
      print(el("pricing-status"), body.detail || "Failed to load prices", true);
      return;
    }
    pricingRows = body.data || [];
    _render();
    if (el("pricing-status")) el("pricing-status").textContent = "";
  } catch (error) {
    print(el("pricing-status"), String(error), true);
  }
}

function _render() {
  const host = el("pricing-list");
  if (!host) return;
  const onlyUnpriced = !!el("pricing-only-unpriced")?.checked;
  const rows = pricingRows
    .filter((r) => RATE_KEYS[r.unit]) // a kind with no priceable unit can't be edited here
    .filter((r) => !onlyUnpriced || !r.price)
    .sort((a, b) => a.kind.localeCompare(b.kind) || a.engine.localeCompare(b.engine) || a.model_id.localeCompare(b.model_id));
  if (!rows.length) {
    host.innerHTML = `<p class="hint">${onlyUnpriced ? "Every model has a price." : "No registry entries yet."}</p>`;
    return;
  }
  host.innerHTML = `
    <table class="data-table">
      <thead>
        <tr>
          <th>Kind</th>
          <th>Engine</th>
          <th>Model</th>
          <th>Unit</th>
          <th>Price (USD)</th>
        </tr>
      </thead>
      <tbody>
        ${rows.map(_renderRow).join("")}
      </tbody>
    </table>`;
}

function _renderRow(row) {
  const inputs = RATE_KEYS[row.unit]
    .map((field) => {
      const value = row.price && row.price[field.key] != null ? String(row.price[field.key]) : "";
      return `
        <label class="price-field">
          <span>${escapeHtml(field.label)}</span>
          <input type="number" class="mini" step="0.000001" min="0"
                 data-price-id="${escapeHtml(row.id)}" data-price-key="${escapeHtml(field.key)}"
                 value="${escapeHtml(value)}" placeholder="0" />
        </label>`;
    })
    .join("");
  return `
    <tr class="${row.price ? "" : "dim"}">
      <td><code>${escapeHtml(row.kind)}</code></td>
      <td>${escapeHtml(row.engine)}</td>
      <td><code>${escapeHtml(row.model_id || "(engine config)")}</code></td>
      <td>${escapeHtml(row.unit)}</td>
      <td>${inputs}</td>
    </tr>`;
}

// One item per row that has at least one non-blank rate input; a row whose
// inputs are all blank is sent as price:null so clearing a price is possible.
function _collect() {
  const byId = new Map();
  document.querySelectorAll("[data-price-id]").forEach((input) => {
    const id = input.getAttribute("data-price-id");
    const key = input.getAttribute("data-price-key");
    const raw = input.value.trim();
    if (!byId.has(id)) byId.set(id, {});
    if (raw !== "") byId.get(id)[key] = Number(raw);
  });
  const items = [];
  for (const [id, price] of byId.entries()) {
    const row = pricingRows.find((r) => r.id === id);
    const hasValues = Object.keys(price).length > 0;
    const hadPrice = !!(row && row.price);
    if (!hasValues && !hadPrice) continue; // nothing to do for an untouched unpriced row
    items.push({ id, price: hasValues ? price : null });
  }
  return items;
}

export async function savePricing() {
  const status = el("pricing-status");
  const items = _collect();
  if (!items.length) {
    print(status, "No prices to save", true);
    return;
  }
  const invalid = items.find((i) => i.price && Object.values(i.price).some((v) => Number.isNaN(v)));
  if (invalid) {
    print(status, "A price field is not a number", true);
    return;
  }
  status.textContent = "Saving…";
  try {
    const resp = await fetch("/v1/model_registry/prices", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prices: items }),
    });
    const body = await resp.json();
    if (!resp.ok) {
      // The server validates all-or-nothing, so nothing was written.
      print(status, body.detail || "Save failed (no prices were changed)", true);
      return;
    }
    status.textContent = `Saved ${body.data.updated} price row(s)`;
    await loadPricing();
  } catch (error) {
    print(status, String(error), true);
  }
}

if (el("pricing-refresh")) el("pricing-refresh").addEventListener("click", loadPricing);
if (el("pricing-save-btn")) el("pricing-save-btn").addEventListener("click", savePricing);
if (el("pricing-only-unpriced")) el("pricing-only-unpriced").addEventListener("change", _render);
