# Usage Dashboard UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** An admin "Usage" tab that reads `/v1/usage/summary` (from Plan 2) and shows aggregated usage — cost, native amount, request count — grouped by user / provider / model / kind / engine, with an optional month filter.

**Architecture:** Static ES-module admin UI (`apps/api_gateway/app/static`), mirroring the Providers tab wiring exactly (a `data-section="usage"` `<li class="admin-only">` nav-item + `#section-usage` + a `loadUsage()` call in `sidebar-nav.js` + side-effect `import` in `main.js`). The summary is a plain read-only HTML table built in `usage.js` (NOT `renderDataTable`, which forces a selection checkbox column that would be dead here).

**Tech Stack:** Vanilla ES modules, `helpers.js` (`el`, `print`, `escapeHtml`). Backend already exists + tested: `GET /v1/usage/summary?group_by=&period=` (admin) returns `{success, data:[{key, cost_usd, native_amount, count}]}`.

## Global Constraints
- **Static-UI only** — no Python edits. Verify with `node --check <file>` + grep; do NOT run pytest.
- Admin-gated: `/v1/usage/summary` is already admin-only (backend); the nav-item must carry `admin-only` on the wrapping `<li>` like the Providers/Model Registry items so non-admins never see the tab.
- Follow existing DOM/class vocabulary verbatim: `section`, `card`, `card-head`, `row tight`, `hint`, `meta`, `ghost`, `mini`, `nav-item`, `admin-only`.
- Git identity `lugondev <lugondev@gmail.com>`. No submodules/.dockerignore. No push (main auto-deploys prod).

---

### Task 1: Usage dashboard tab (summary table + group-by/period controls)

**Files:**
- Create: `apps/api_gateway/app/static/js/usage.js`
- Modify: `apps/api_gateway/app/static/index.html` (nav-item after the Providers item's `</li>`; `<section id="section-usage">` after `#section-providers` closes)
- Modify: `apps/api_gateway/app/static/js/sidebar-nav.js` (import + activation call)
- Modify: `apps/api_gateway/app/static/js/main.js` (side-effect import)

**Interfaces:**
- Produces: `export async function loadUsage()` — reads the controls, fetches `/v1/usage/summary`, renders the table into `#usage-list`.

- [ ] **Step 1: Add the nav-item** in `index.html`, immediately AFTER the Providers item's closing `</li>` (find the `<li class="admin-only">` wrapping `data-section="providers"` and insert after its `</li>`). Match the sibling structure exactly:

```html
            <li class="admin-only">
              <button class="nav-item" data-section="usage">
                <span class="nav-icon">&#128202;</span>
                <span class="nav-label">Usage</span>
              </button>
            </li>
```

- [ ] **Step 2: Add the section** in `index.html`, immediately AFTER `#section-providers`'s closing `</div>`:

```html
          <!-- ============================== USAGE ============================== -->
          <div class="section" id="section-usage">
            <section class="card">
              <div class="card-head">
                <h2>Usage</h2>
                <button id="usage-refresh" class="ghost mini">Refresh</button>
              </div>
              <p class="hint">Recorded usage (LLM tokens, STT seconds, TTS characters) and cost, aggregated. Cost is $0 for models without a configured price.</p>
              <div class="row tight">
                <label>
                  Group by
                  <select id="usage-group-by">
                    <option value="provider">Provider</option>
                    <option value="model">Model</option>
                    <option value="kind">Kind</option>
                    <option value="engine">Engine</option>
                    <option value="user">User</option>
                  </select>
                </label>
                <label>
                  Month (optional)
                  <input id="usage-period" type="text" placeholder="YYYY-MM (blank = all time)" />
                </label>
              </div>
              <div id="usage-list" class="model-list">
                <p class="hint">Loading&#8230;</p>
              </div>
              <p id="usage-status" class="meta"></p>
            </section>
          </div>
```

- [ ] **Step 3: Create `usage.js`** with the full content:

```javascript
import { el, print, escapeHtml } from "./helpers.js";

// Column header for the "key" varies with the group-by dimension.
const _KEY_LABEL = {
  provider: "Provider ID", model: "Model", kind: "Kind", engine: "Engine", user: "User ID",
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
            <td><code>${escapeHtml(String(r.key || "") || "(none)")}</code></td>
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
```

- [ ] **Step 4: Wire `sidebar-nav.js`** — add the import and activation call (mirror `providers`):

```javascript
import { loadUsage } from "./usage.js";
```
and inside `activateSection`, after the `providers` line:
```javascript
  if (section === "usage") loadUsage();
```

- [ ] **Step 5: Wire `main.js`** — after `import "./providers.js";` add:

```javascript
import "./usage.js";
```

- [ ] **Step 6: Verify (static-UI checks)**

```bash
cd /Users/lugon/code/speech-text-transformer
node --check apps/api_gateway/app/static/js/usage.js && echo "usage.js OK"
node --check apps/api_gateway/app/static/js/sidebar-nav.js && echo "sidebar-nav.js OK"
node --check apps/api_gateway/app/static/js/main.js && echo "main.js OK"
grep -n "section-usage\|data-section=\"usage\"" apps/api_gateway/app/static/index.html
grep -c "class=\"data-table\"" apps/api_gateway/app/static/styles.css || echo "NOTE: verify .data-table is styled (it is used by other tables); a plain <table> still renders if not."
```
Expected: three "OK"; grep shows nav-item + section. (`.data-table` is the class the existing `renderDataTable` output uses, so it is already styled.)

- [ ] **Step 7: Commit**

```bash
git add apps/api_gateway/app/static/js/usage.js apps/api_gateway/app/static/index.html apps/api_gateway/app/static/js/sidebar-nav.js apps/api_gateway/app/static/js/main.js
git -c user.name=lugondev -c user.email=lugondev@gmail.com commit -m "feat(admin-ui): Usage dashboard tab (summary by provider/model/kind/engine/user)"
```

---

### Task 2: Browser smoke (controller-run, optional)
- [ ] With the app running + logged in as admin, open the Usage tab, confirm the table loads, switch Group by, enter a month. If no running instance, rely on the `node --check` + grep gates and note it.

---

## Self-Review
- **Spec coverage:** admin Usage tab (T1) ✓; consumes `/v1/usage/summary` with group_by + period controls ✓; read-only table with totals ✓; admin-only nav (li.admin-only) ✓; static-only verification ✓.
- **Placeholder scan:** complete code; Step 1/2 require reading the Providers nav-item/section to place precisely (structure given).
- **Consistency:** IDs in usage.js (`usage-list`, `usage-group-by`, `usage-period`, `usage-status`, `usage-refresh`) match the HTML; `loadUsage` exported + imported in sidebar-nav; side-effect import in main.js. group_by option values (provider/model/kind/engine/user) match the backend `summarize` accepted keys. The `key` field name matches the backend row shape `{key, cost_usd, native_amount, count}`.
