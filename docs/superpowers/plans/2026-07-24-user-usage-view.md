# User "My Usage" View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Give a logged-in (non-admin) user a UI to view their OWN usage, consuming the existing `GET /v1/usage/me` — on BOTH surfaces: the static admin console (a non-admin "My Usage" tab) and the end-user React SPA `lugo-web-client` (a "My Usage" screen).

**Backend (already done):** `GET /v1/usage/me?period=YYYY-MM` → `{"success": true, "data": [{kind, model_id, cost_usd, native_amount, count}]}`, scoped server-side to the caller (any logged-in user; returns only their own rows). Carved out of the admin `/v1/usage` prefix.

**Architecture:** Two independent, read-only front-end additions. T1 = vanilla ES-module tab in `apps/api_gateway/app/static` (mirrors the existing `usage.js` admin dashboard but non-admin + calls `/me`, no group-by). T2 = a React screen in the `lugo-web-client` submodule (mirrors the `History` screen; adds an `src/api/usage.ts` client fn; registers a nav entry) + a superproject submodule-pointer bump.

## Global Constraints
- **Front-end only** — no Python. T1 verify: `node --check` + grep (NO pytest). T2 verify: inside `lugo-web-client`: `pnpm lint && npx tsc -b && pnpm test`.
- `/v1/usage/me` is reachable by ANY logged-in user (already gated in `_USER_PREFIXES`); the "My Usage" tab/screen must NOT be admin-only.
- Static UI vocabulary: `section`, `card`, `card-head`, `row tight`, `hint`, `meta`, `ghost`, `mini`, `nav-item`, `.data-table` (styled). Non-admin nav item = a PLAIN `<li>` (NOT `<li class="admin-only">`).
- `lugo-web-client` is a git SUBMODULE: T2 commits happen INSIDE it (its own repo), then the superproject records the new pointer with a separate commit. Bearer-token auth via `apiFetch` (never raw fetch). TypeScript + vitest + oxlint; pnpm.
- Git identity `lugondev <lugondev@gmail.com>`. Do NOT push (main auto-deploys prod). Concurrent session active — re-check `git branch --show-current` before git-mutating steps in the superproject.

---

### Task 1: Static UI "My Usage" tab (non-admin)

**Files:**
- Create: `apps/api_gateway/app/static/js/usage-me.js`
- Modify: `apps/api_gateway/app/static/index.html` (a PLAIN-`<li>` nav-item + `#section-my-usage`)
- Modify: `apps/api_gateway/app/static/js/sidebar-nav.js`, `apps/api_gateway/app/static/js/main.js`

**Interfaces:** `export async function loadMyUsage()` — fetch `/v1/usage/me` (+ optional `?period`), render a read-only table (Kind, Model, Cost USD, Native amount, Requests) + totals into `#my-usage-list`.

- [ ] **Step 1: Nav-item** — in `index.html`, add a NON-admin nav item. Place it near the other user-visible tabs (e.g. right after the `devices` or `history`-style user tabs; if unsure, place it immediately BEFORE the first `<li class="admin-only">`). It must be a PLAIN `<li>` (no `admin-only`), mirroring a user tab's structure:

```html
            <li>
              <button class="nav-item" data-section="my-usage">
                <span class="nav-icon">&#128200;</span>
                <span class="nav-label">My Usage</span>
              </button>
            </li>
```
Read the nav list first to copy the exact user-tab `<li>`/`<button>`/`<span>` structure and confirm whether user tabs use a plain `<li>`.

- [ ] **Step 2: Section** — add after the last existing `#section-*` block (e.g. after `#section-quotas`'s closing `</div>`, or anywhere among the sections):

```html
          <!-- ============================== MY USAGE ============================== -->
          <div class="section" id="section-my-usage">
            <section class="card">
              <div class="card-head">
                <h2>My Usage</h2>
                <button id="my-usage-refresh" class="ghost mini">Refresh</button>
              </div>
              <p class="hint">Your own recorded usage (LLM tokens, STT seconds, TTS characters) and cost. Cost is $0 for models without a configured price.</p>
              <div class="row tight">
                <label>
                  Month (optional)
                  <input id="my-usage-period" type="text" placeholder="YYYY-MM (blank = all time)" />
                </label>
              </div>
              <div id="my-usage-list" class="model-list">
                <p class="hint">Loading&#8230;</p>
              </div>
              <p id="my-usage-status" class="meta"></p>
            </section>
          </div>
```

- [ ] **Step 3: Create `usage-me.js`**

```javascript
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
    _render(host, body.data || []);
    if (status) status.textContent = "";
  } catch (error) {
    print(status, String(error), true);
  }
}

function _render(host, rows) {
  if (!rows.length) {
    host.innerHTML = `<p class="hint">No usage recorded${el("my-usage-period")?.value ? " for that month" : ""} yet.</p>`;
    return;
  }
  const sorted = [...rows].sort((a, b) => Number(b.cost_usd || 0) - Number(a.cost_usd || 0));
  const tc = sorted.reduce((s, r) => s + Number(r.cost_usd || 0), 0);
  const tn = sorted.reduce((s, r) => s + Number(r.native_amount || 0), 0);
  const tq = sorted.reduce((s, r) => s + Number(r.count || 0), 0);
  host.innerHTML = `
    <table class="data-table">
      <thead>
        <tr><th>Kind</th><th>Model</th><th>Cost (USD)</th><th>Native amount</th><th>Requests</th></tr>
      </thead>
      <tbody>
        ${sorted.map((r) => `
          <tr>
            <td>${escapeHtml(String(r.kind || ""))}</td>
            <td><code>${escapeHtml(String(r.model_id || "") || "(none)")}</code></td>
            <td>${_fmtCost(r.cost_usd)}</td>
            <td>${_fmtNum(r.native_amount)}</td>
            <td>${_fmtNum(r.count)}</td>
          </tr>`).join("")}
      </tbody>
      <tfoot>
        <tr>
          <td colspan="2"><strong>Total</strong></td>
          <td><strong>${_fmtCost(tc)}</strong></td>
          <td><strong>${_fmtNum(tn)}</strong></td>
          <td><strong>${_fmtNum(tq)}</strong></td>
        </tr>
      </tfoot>
    </table>`;
}

if (el("my-usage-refresh")) el("my-usage-refresh").addEventListener("click", loadMyUsage);
if (el("my-usage-period")) el("my-usage-period").addEventListener("change", loadMyUsage);
```
(Note: rows are grouped by (kind, model_id) so the per-row native_amount is unit-consistent within a row; the total mixes units across kinds — acceptable for a personal at-a-glance view, same tradeoff as the admin dashboard.)

- [ ] **Step 4: Wire `sidebar-nav.js`** — `import { loadMyUsage } from "./usage-me.js";` + `if (section === "my-usage") loadMyUsage();` in activateSection.

- [ ] **Step 5: Wire `main.js`** — `import "./usage-me.js";` with the other side-effect imports.

- [ ] **Step 6: Verify** — `node --check` on usage-me.js/sidebar-nav.js/main.js (all OK); grep `section-my-usage` + `data-section="my-usage"` present; confirm the nav `<li>` is NOT `admin-only`.

- [ ] **Step 7: Commit** — `git add` the 4 files → `feat(admin-ui): non-admin 'My Usage' tab (per-user usage view)`.

---

### Task 2: `lugo-web-client` "My Usage" screen (React submodule) + superproject pointer bump

**Files (inside the `lugo-web-client` submodule):**
- Create: `lugo-web-client/src/api/usage.ts`, `lugo-web-client/src/screens/Usage.tsx`, `lugo-web-client/src/screens/Usage.css`
- Modify: `lugo-web-client/src/components/Nav.tsx`, `lugo-web-client/src/App.tsx`
- (Optional) Create: `lugo-web-client/src/api/usage.test.ts`
- Then in the SUPERPROJECT: `git add lugo-web-client` (pointer bump) + commit.

**Interfaces:** `getMyUsage(period?: string): Promise<UsageRow[]>` (via `apiFetch`), `Usage` screen registered as `Screen` `'usage'`.

- [ ] **Step 1: Read** `lugo-web-client/src/api/history.ts`, `lugo-web-client/src/screens/History.tsx`, `lugo-web-client/src/api/client.ts`, `lugo-web-client/src/components/Nav.tsx`, `lugo-web-client/src/App.tsx` to confirm the exact patterns (apiFetch signature, Screen union, ITEMS list, SCREENS map, how History fetches on mount + renders + its CSS import). Follow them verbatim.

- [ ] **Step 2: `src/api/usage.ts`**

```ts
import { apiFetch } from './client'

export type UsageRow = {
  kind: string
  model_id: string
  cost_usd: number
  native_amount: number
  count: number
}

export async function getMyUsage(period?: string): Promise<UsageRow[]> {
  const qs = period ? `?period=${encodeURIComponent(period)}` : ''
  const resp = await apiFetch(`/v1/usage/me${qs}`)
  if (!resp.ok) throw new Error(`Server returned error ${resp.status}`)
  const body = await resp.json()
  return (body.data ?? []) as UsageRow[]
}
```
(Confirm `apiFetch` is exported from `./client` with signature `apiFetch(path, init?)` — per the codebase map it is.)

- [ ] **Step 3: `src/screens/Usage.tsx`** — model on `History.tsx`'s top-level component: `rows`/`error` state, `useEffect(() => { void refresh() }, [])`, a `refresh(period?)` calling `getMyUsage`, render a table + totals. Read `History.tsx` first and match its className/error/empty conventions. Skeleton (adapt classNames to match History's BEM style + its actual imports):

```tsx
import { useEffect, useState } from 'react'
import { getMyUsage, type UsageRow } from '../api/usage'
import './Usage.css'

export function Usage() {
  const [rows, setRows] = useState<UsageRow[]>([])
  const [error, setError] = useState<string | null>(null)
  const [period, setPeriod] = useState('')

  async function refresh(p: string = period) {
    try {
      setError(null)
      setRows(await getMyUsage(p.trim() || undefined))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }
  useEffect(() => { void refresh() }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const totalCost = rows.reduce((s, r) => s + (r.cost_usd || 0), 0)
  const totalCount = rows.reduce((s, r) => s + (r.count || 0), 0)

  return (
    <div className="usage">
      <div className="usage__head">
        <h2>My Usage</h2>
        <label>
          Month{' '}
          <input
            value={period}
            placeholder="YYYY-MM"
            onChange={(e) => setPeriod(e.target.value)}
            onBlur={() => void refresh()}
          />
        </label>
      </div>
      {error && <p className="usage__err" role="alert">{error}</p>}
      {!error && rows.length === 0 ? (
        <p className="usage__empty">No usage recorded yet.</p>
      ) : (
        <table className="usage__table">
          <thead>
            <tr><th>Kind</th><th>Model</th><th>Cost (USD)</th><th>Amount</th><th>Requests</th></tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.kind}-${r.model_id}`}>
                <td>{r.kind}</td>
                <td><code>{r.model_id || '(none)'}</code></td>
                <td>${(r.cost_usd || 0).toFixed(4)}</td>
                <td>{(r.native_amount || 0).toLocaleString()}</td>
                <td>{(r.count || 0).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <td colSpan={2}><strong>Total</strong></td>
              <td><strong>${totalCost.toFixed(4)}</strong></td>
              <td></td>
              <td><strong>{totalCount.toLocaleString()}</strong></td>
            </tr>
          </tfoot>
        </table>
      )}
    </div>
  )
}
```

- [ ] **Step 4: `src/screens/Usage.css`** — a minimal stylesheet matching the app's look (mirror `History.css` conventions: table width 100%, cell padding, a muted `.usage__empty`/`.usage__err`). Keep it small.

- [ ] **Step 5: Register the screen** —
  - `src/components/Nav.tsx`: add `'usage'` to the `Screen` union type; add `{ id: 'usage', label: 'My Usage' }` to `ITEMS`.
  - `src/App.tsx`: `import { Usage } from './screens/Usage'`; add `usage: Usage,` to the `SCREENS` map (prop-less, renders via the default branch).

- [ ] **Step 6: (Optional) `src/api/usage.test.ts`** — mirror `src/api/history.test.ts`: stub `fetch`/token, assert `getMyUsage()` hits `/v1/usage/me`, sends the `Bearer` header (via apiFetch), and returns `body.data`. Add a `?period=` case.

- [ ] **Step 7: Verify (inside the submodule)**

```bash
cd /Users/lugon/code/speech-text-transformer/lugo-web-client
pnpm lint && npx tsc -b && pnpm test
```
Expected: lint clean, tsc clean, vitest all pass.

- [ ] **Step 8: Commit in the submodule, then bump the superproject pointer**

```bash
cd /Users/lugon/code/speech-text-transformer/lugo-web-client
git add src/api/usage.ts src/screens/Usage.tsx src/screens/Usage.css src/components/Nav.tsx src/App.tsx src/api/usage.test.ts
git -c user.name=lugondev -c user.email=lugondev@gmail.com commit -m "feat(usage): My Usage screen (per-user usage view via /v1/usage/me)"
cd /Users/lugon/code/speech-text-transformer
git branch --show-current   # confirm still on feat/user-usage-view before touching the superproject
git add lugo-web-client
git -c user.name=lugondev -c user.email=lugondev@gmail.com commit -m "chore: bump lugo-web-client (My Usage screen)"
```
(Do NOT push the submodule or the superproject.)

---

### Task 3: Verify both (controller)
- [ ] Static: `node --check` on the 3 static JS files; grep the new IDs. React: `cd lugo-web-client && pnpm lint && npx tsc -b && pnpm test`. Backend `.venv/bin/python -c "import app.main"` (import unaffected — no Python changed).

## Self-Review
- **Coverage:** admin already had management + admin usage dashboard; this adds the missing USER self-view on BOTH surfaces (static non-admin tab T1 + React screen T2), consuming the pre-built `/v1/usage/me`. Non-admin reachability preserved (plain `<li>` / no guard needed in React since the whole app is behind login).
- **Placeholder scan:** complete code for T1 + T2 api/screen; T2 CSS + nav registration are "read History/Nav first, mirror" steps with the exact edit points named.
- **Consistency:** `/v1/usage/me` envelope `{success,data:[{kind,model_id,cost_usd,native_amount,count}]}` consumed identically in T1 (`body.data`) and T2 (`body.data`); IDs (`my-usage-*`) match between usage-me.js and index.html; React screen registered per the mapped Nav/App pattern; submodule commit precedes the superproject pointer bump.
