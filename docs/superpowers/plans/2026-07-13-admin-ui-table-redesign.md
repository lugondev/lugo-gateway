# Admin UI Table + Bulk-Select Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the flex `.model-row` card-list pattern on five admin pages (Users, Model Registry, TTS Profiles, MCP Servers, Devices) with a real `<table>` + checkbox multi-select + bulk actions, via one shared vanilla-JS component.

**Architecture:** A new `renderDataTable()` helper (`apps/api_gateway/app/static/js/data-table.js`) renders a `<table>` with a checkbox column and an optional bulk-action toolbar that appears above the table when rows are selected. Each page module keeps its existing fetch/update logic and only rewrites its `render*()` function to build a `columns` array and call `renderDataTable()`. Bulk actions loop client-side over the *existing* per-item PATCH/DELETE/POST endpoints — no new backend routes.

**Tech Stack:** Vanilla ES modules (no build step, no framework), FastAPI backend (unchanged), plain CSS with existing custom-property design tokens in `apps/api_gateway/app/static/styles.css`.

## Global Constraints

- No new runtime dependencies and no frontend framework — stay vanilla ES modules, matching every other file in `apps/api_gateway/app/static/js/`.
- No new backend endpoints or fields. Bulk actions must only call verbs that already exist (confirmed by reading the routers): Users and Model Registry have no DELETE — their "remove" bulk action is disable, not delete.
- No pagination, sorting, or search/filter in this plan.
- The **Profiles** page (`profile-select` dropdown + `profile-panel` on the Chat tab) is explicitly **out of scope** — it's a picker+panel UI, not a list page, and is not touched by this plan.
- This repo has no JavaScript test runner (no `package.json` at all). Verification for every task is manual: start the server, drive the page in a browser, confirm the checklist in that task's step. This is a deliberate deviation from the literal pytest-style TDD loop — there is nothing to automate here.
- One-time setup before the first manual verification: `make install` (installs the package + dev deps into `.venv`; this pulls the project's ML dependencies and can take several minutes — only needs to run once per worktree). Start the server for manual checks with `make start` (serves the admin UI at `http://localhost:8000/static/index.html`), stop it afterward with `make stop`.
- Every task must leave `git status` clean (all changes committed) before moving to the next task.

---

### Task 1: Shared data-table component + Users page

**Files:**
- Create: `apps/api_gateway/app/static/js/data-table.js`
- Modify: `apps/api_gateway/app/static/js/helpers.js`
- Modify: `apps/api_gateway/app/static/styles.css`
- Modify: `apps/api_gateway/app/static/js/users.js`

**Interfaces:**
- Produces (used by every later task):
  - `helpers.js` → `escapeHtml(str): string`, `runBulk(ids: string[], fn: (id: string) => Promise<{ok: boolean, error?: string}>, describeId: (id: string) => string): Promise<string[]>` (returns formatted error lines, empty if all succeeded), `printBulkSummary(statusEl: Element, total: number, errors: string[], verb?: string): void`.
  - `data-table.js` → `renderDataTable(opts): HTMLTableElement | null` where `opts = { container: Element, columns: {key: string, label: string, render: (row) => string, headerClass?: string, cellClass?: string}[], rows: any[], rowKey: (row) => string, getRowClass?: (row) => string, bulkActions?: {label: string, run: (ids: string[], selectedRows: any[]) => void}[], emptyMessage?: string }`. Returns the built `<table>` element (so the caller can `querySelectorAll` its own interactive controls and attach listeners), or `null` when `rows` is empty (in which case `container.innerHTML` is already set to `emptyMessage`).
- Consumes: nothing new — `users.js` keeps its existing exports (`userData`, `loadUsers`, `createUser`) unchanged so `main.js`/`sidebar-nav.js` need no changes.

- [ ] **Step 1: Add `escapeHtml`, `runBulk`, `printBulkSummary` to `helpers.js`**

Append to `apps/api_gateway/app/static/js/helpers.js` (after the existing `setBadge` export):

```js
export function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML;
}

// Runs `fn(id)` for every id in sequence. `fn` must resolve to
// `{ ok: true }` or `{ ok: false, error }` and must never throw (catch
// internally). Returns one "<label>: <error>" string per failed id — an
// empty array means every id succeeded.
export async function runBulk(ids, fn, describeId) {
  const errors = [];
  for (const id of ids) {
    const result = await fn(id);
    if (!result.ok) errors.push(`${describeId(id)}: ${result.error}`);
  }
  return errors;
}

// Prints a one-line summary of a completed bulk action to `statusEl`.
export function printBulkSummary(statusEl, total, errors, verb = "Updated") {
  if (errors.length) {
    print(statusEl, `${errors.length} of ${total} failed:\n${errors.join("\n")}`, true);
  } else {
    print(statusEl, `${verb} ${total} item${total === 1 ? "" : "s"}`);
  }
}
```

- [ ] **Step 2: Create `data-table.js`**

Create `apps/api_gateway/app/static/js/data-table.js`:

```js
import { escapeHtml } from "./helpers.js";

// Renders a checkbox-selectable table into `container`. Returns the built
// <table> (so callers can wire up their own per-column controls), or null
// if there are no rows (container is filled with `emptyMessage` instead).
export function renderDataTable({
  container,
  columns,
  rows,
  rowKey,
  getRowClass,
  bulkActions = [],
  emptyMessage = "No entries yet.",
}) {
  if (!container) return null;
  if (!rows.length) {
    container.innerHTML = `<p class="hint">${escapeHtml(emptyMessage)}</p>`;
    return null;
  }

  const selected = new Set();
  const toolbar = document.createElement("div");
  const table = document.createElement("table");
  table.className = "data-table";

  const thead = document.createElement("thead");
  thead.innerHTML = `
    <tr>
      <th class="dt-checkbox-cell"><input type="checkbox" /></th>
      ${columns.map((c) => `<th${c.headerClass ? ` class="${c.headerClass}"` : ""}>${escapeHtml(c.label)}</th>`).join("")}
    </tr>
  `;
  const selectAllCheckbox = thead.querySelector("input");

  const tbody = document.createElement("tbody");
  tbody.innerHTML = rows.map((row) => `
    <tr class="${getRowClass ? getRowClass(row) : ""}">
      <td class="dt-checkbox-cell"><input type="checkbox" /></td>
      ${columns.map((c) => `<td${c.cellClass ? ` class="${c.cellClass}"` : ""}>${c.render(row)}</td>`).join("")}
    </tr>
  `).join("");

  table.append(thead, tbody);

  function renderToolbar() {
    if (selected.size === 0) {
      toolbar.innerHTML = "";
      toolbar.classList.remove("dt-toolbar");
      return;
    }
    toolbar.classList.add("dt-toolbar");
    toolbar.innerHTML = `
      <span class="dt-toolbar-count">${selected.size} selected</span>
      <div class="dt-toolbar-actions"></div>
    `;
    const actionsHost = toolbar.querySelector(".dt-toolbar-actions");
    bulkActions.forEach((action) => {
      const btn = document.createElement("button");
      btn.className = "mini ghost";
      btn.type = "button";
      btn.textContent = action.label;
      btn.addEventListener("click", () => {
        const ids = [...selected];
        const selectedRows = rows.filter((r) => selected.has(rowKey(r)));
        action.run(ids, selectedRows);
      });
      actionsHost.appendChild(btn);
    });
  }

  function updateSelectAllState() {
    selectAllCheckbox.checked = selected.size > 0 && selected.size === rows.length;
    selectAllCheckbox.indeterminate = selected.size > 0 && selected.size < rows.length;
  }

  [...tbody.children].forEach((tr, i) => {
    const id = rowKey(rows[i]);
    const cb = tr.querySelector("input[type=checkbox]");
    cb.addEventListener("change", () => {
      if (cb.checked) selected.add(id); else selected.delete(id);
      tr.classList.toggle("dt-row-selected", cb.checked);
      updateSelectAllState();
      renderToolbar();
    });
  });

  selectAllCheckbox.addEventListener("change", () => {
    const checked = selectAllCheckbox.checked;
    selected.clear();
    [...tbody.children].forEach((tr, i) => {
      const cb = tr.querySelector("input[type=checkbox]");
      cb.checked = checked;
      tr.classList.toggle("dt-row-selected", checked);
      if (checked) selected.add(rowKey(rows[i]));
    });
    updateSelectAllState();
    renderToolbar();
  });

  container.innerHTML = "";
  container.append(toolbar, table);
  return table;
}
```

- [ ] **Step 3: Add `.data-table` / `.dt-*` CSS**

In `apps/api_gateway/app/static/styles.css`, insert this new block immediately after the closing `}` of `.model-action` (the block right before `.progress {` — search for `.model-action {` around line 901, insert after its closing brace):

```css
/* ================================================================
   DATA TABLE
   ================================================================ */

.data-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.data-table thead th {
  text-align: left;
  padding: 8px 10px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--muted);
  border-bottom: 1px solid var(--line);
  white-space: nowrap;
}
.data-table tbody td {
  padding: 8px 10px;
  vertical-align: middle;
  border-bottom: 1px solid var(--line);
}
.data-table tbody tr {
  transition: background-color 140ms;
}
.data-table tbody tr:hover {
  background: var(--surface-hover);
}
.data-table tbody tr.dim {
  opacity: 0.4;
}
.data-table tbody tr.dt-row-selected {
  background: rgba(125, 234, 214, 0.08);
}
.data-table .dt-checkbox-cell {
  width: 28px;
  padding-right: 0;
}
.data-table .dt-actions-cell {
  text-align: right;
  white-space: nowrap;
}
.data-table .dt-actions-cell button + button {
  margin-left: 6px;
}

.dt-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 14px;
  margin-bottom: 8px;
  border: 1px solid rgba(125, 234, 214, 0.28);
  border-radius: var(--r-md);
  background: rgba(125, 234, 214, 0.06);
}
.dt-toolbar-count {
  font-size: 12px;
  font-weight: 600;
  color: var(--text);
}
.dt-toolbar-actions {
  display: flex;
  gap: 6px;
}
```

Before moving on, load the `frontend-design` skill and review this CSS block against the rest of `styles.css` (tokens, spacing scale, hover treatment) — this is the one shared visual surface every later task inherits, so get it right here rather than re-touching it per page.

- [ ] **Step 4: Rewrite `users.js` to use the data table**

Replace the full contents of `apps/api_gateway/app/static/js/users.js` with:

```js
import { el, print, escapeHtml, runBulk, printBulkSummary } from "./helpers.js";
import { renderDataTable } from "./data-table.js";

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

function renderUserList() {
  const host = el("user-list");
  if (!host) return;

  const table = renderDataTable({
    container: host,
    rows: userData,
    rowKey: (u) => u.id,
    getRowClass: (u) => (u.disabled ? "dim" : ""),
    emptyMessage: "No users yet.",
    columns: [
      { key: "username", label: "Username", render: (u) => `<strong>${escapeHtml(u.username)}</strong>` },
      {
        key: "role",
        label: "Role",
        render: (u) => `
          <select data-user-role="${escapeHtml(u.id)}">
            <option value="user" ${u.role === "user" ? "selected" : ""}>user</option>
            <option value="admin" ${u.role === "admin" ? "selected" : ""}>admin</option>
          </select>
        `,
      },
      {
        key: "testing",
        label: "Testing",
        render: (u) => `<input type="checkbox" data-user-testing="${escapeHtml(u.id)}" ${u.can_use_testing ? "checked" : ""} />`,
      },
      { key: "status", label: "Status", render: (u) => (u.disabled ? "Disabled" : "Active") },
      {
        key: "actions",
        label: "",
        headerClass: "dt-actions-cell",
        cellClass: "dt-actions-cell",
        render: (u) => `
          <button class="mini" data-user-toggle-disabled="${escapeHtml(u.id)}">${u.disabled ? "Enable" : "Disable"}</button>
          <button class="mini" data-user-reset="${escapeHtml(u.id)}">Reset password</button>
        `,
      },
    ],
    bulkActions: [
      { label: "Disable selected", run: (ids) => bulkUpdateUsers(ids, { disabled: true }, "Disabled") },
      { label: "Enable selected", run: (ids) => bulkUpdateUsers(ids, { disabled: false }, "Enabled") },
      { label: "Make admin", run: (ids) => bulkUpdateUsers(ids, { role: "admin" }, "Updated") },
      { label: "Make user", run: (ids) => bulkUpdateUsers(ids, { role: "user" }, "Updated") },
    ],
  });
  if (!table) return;

  table.querySelectorAll("[data-user-role]").forEach((sel) =>
    sel.addEventListener("change", () => updateUser(sel.getAttribute("data-user-role"), { role: sel.value }))
  );
  table.querySelectorAll("[data-user-testing]").forEach((cb) =>
    cb.addEventListener("change", () =>
      updateUser(cb.getAttribute("data-user-testing"), { can_use_testing: cb.checked })
    )
  );
  table.querySelectorAll("[data-user-toggle-disabled]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-user-toggle-disabled");
      const user = userData.find((u) => u.id === id);
      updateUser(id, { disabled: !user.disabled });
    })
  );
  table.querySelectorAll("[data-user-reset]").forEach((btn) =>
    btn.addEventListener("click", () => resetUserPassword(btn.getAttribute("data-user-reset")))
  );
}

async function _patchUserRaw(id, fields) {
  try {
    const resp = await fetch(`/v1/users/${encodeURIComponent(id)}`, {
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

async function updateUser(id, fields) {
  const result = await _patchUserRaw(id, fields);
  if (!result.ok) {
    print(el("user-status"), result.error, true);
    return;
  }
  await loadUsers();
}

async function bulkUpdateUsers(ids, fields, verb) {
  const errors = await runBulk(
    ids,
    (id) => _patchUserRaw(id, fields),
    (id) => userData.find((u) => u.id === id)?.username || id
  );
  await loadUsers();
  printBulkSummary(el("user-status"), ids.length, errors, verb);
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
```

- [ ] **Step 5: Manually verify in the browser**

Run once if not already installed: `make install` (skip if `.venv` already has deps). Then:

```bash
make start
```

Open `http://localhost:8000/static/index.html`, log in as admin, go to the **Users** section, and confirm:
- The user list renders as a `<table>` with columns Username / Role / Testing / Status / (actions), not the old flex rows.
- Clicking the header checkbox checks every row and shows a toolbar reading "N selected" with four buttons: Disable selected, Enable selected, Make admin, Make user. Clicking it again unchecks every row and the toolbar disappears.
- Checking exactly one row's checkbox (with more than one user present) leaves the header checkbox showing as indeterminate (a dash, not empty or checked).
- With two non-admin users selected, click "Disable selected" — both rows go dim and show "Disabled" / an "Enable" button, and the status line reads "Disabled 2 items".
- Select every admin user and click "Disable selected" — the response includes a line like "admin: cannot remove the last active admin" for whichever admin the server's last-admin safeguard rejects, while any other selected non-admin still gets disabled.
- The existing single-row controls (Role select, Testing checkbox, Enable/Disable button, Reset password button) still work exactly as before.

Then run `make stop`.

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/static/js/data-table.js apps/api_gateway/app/static/js/helpers.js apps/api_gateway/app/static/js/users.js apps/api_gateway/app/static/styles.css
git commit -m "feat(ui): add shared data-table component and migrate Users to it"
```

---

### Task 2: Model Registry page

**Files:**
- Modify: `apps/api_gateway/app/static/js/model-registry.js`

**Interfaces:**
- Consumes: `escapeHtml`, `runBulk`, `printBulkSummary` from `./helpers.js` (Task 1); `renderDataTable` from `./data-table.js` (Task 1).
- Produces: no change to `registryData`, `loadModelRegistry`, `createModelRegistryEntry` exports — other files keep importing them unchanged.

- [ ] **Step 1: Rewrite `model-registry.js`**

Replace the full contents of `apps/api_gateway/app/static/js/model-registry.js` with:

```js
import { el, print, escapeHtml, runBulk, printBulkSummary } from "./helpers.js";
import { renderDataTable } from "./data-table.js";

export let registryData = [];

export async function loadModelRegistry() {
  try {
    const body = await (await fetch("/v1/model_registry")).json();
    registryData = body.data || [];
    renderModelRegistry();
  } catch {
    /* ignore */
  }
}

function renderModelRegistry() {
  const host = el("model-registry-list");
  if (!host) return;

  const table = renderDataTable({
    container: host,
    rows: registryData,
    rowKey: (e) => e.id,
    getRowClass: (e) => (e.enabled ? "" : "dim"),
    emptyMessage: "No entries yet.",
    columns: [
      { key: "kind", label: "Kind", render: (e) => `<strong>${escapeHtml(e.kind)}</strong>` },
      { key: "model", label: "Engine / Model", render: (e) => `<code>${escapeHtml(e.engine)}/${escapeHtml(e.model_id)}</code>` },
      { key: "label", label: "Label", render: (e) => escapeHtml(e.label) },
      {
        key: "stage",
        label: "Stage",
        render: (e) => `
          <select data-registry-stage="${escapeHtml(e.id)}">
            <option value="stable" ${e.stage === "stable" ? "selected" : ""}>stable</option>
            <option value="testing" ${e.stage === "testing" ? "selected" : ""}>testing</option>
          </select>
        `,
      },
      {
        key: "actions",
        label: "",
        headerClass: "dt-actions-cell",
        cellClass: "dt-actions-cell",
        render: (e) => `<button class="mini" data-registry-toggle="${escapeHtml(e.id)}">${e.enabled ? "Disable" : "Enable"}</button>`,
      },
    ],
    bulkActions: [
      { label: "Enable selected", run: (ids) => bulkPatchEntries(ids, { enabled: true }, "Enabled") },
      { label: "Disable selected", run: (ids) => bulkPatchEntries(ids, { enabled: false }, "Disabled") },
      { label: "Set stage: stable", run: (ids) => bulkPatchEntries(ids, { stage: "stable" }, "Updated") },
      { label: "Set stage: testing", run: (ids) => bulkPatchEntries(ids, { stage: "testing" }, "Updated") },
    ],
  });
  if (!table) return;

  table.querySelectorAll("[data-registry-stage]").forEach((sel) =>
    sel.addEventListener("change", () =>
      patchEntry(sel.getAttribute("data-registry-stage"), { stage: sel.value })
    )
  );
  table.querySelectorAll("[data-registry-toggle]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-registry-toggle");
      const entry = registryData.find((e) => e.id === id);
      patchEntry(id, { enabled: !entry.enabled });
    })
  );
}

async function _patchEntryRaw(id, fields) {
  try {
    const resp = await fetch(`/v1/model_registry/${encodeURIComponent(id)}`, {
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

async function patchEntry(id, fields) {
  const result = await _patchEntryRaw(id, fields);
  if (!result.ok) {
    print(el("model-registry-status"), result.error, true);
    return;
  }
  await loadModelRegistry();
}

async function bulkPatchEntries(ids, fields, verb) {
  const errors = await runBulk(
    ids,
    (id) => _patchEntryRaw(id, fields),
    (id) => registryData.find((e) => e.id === id)?.label || id
  );
  await loadModelRegistry();
  printBulkSummary(el("model-registry-status"), ids.length, errors, verb);
}

function _updateKindFields() {
  const kind = el("registry-add-kind").value;
  el("registry-add-llm-fields").classList.toggle("hidden", kind !== "llm");
}

export async function createModelRegistryEntry() {
  const status = el("model-registry-status");
  const kind = el("registry-add-kind").value;
  const engine = el("registry-add-engine").value.trim();
  const modelId = el("registry-add-model-id").value.trim();
  const label = el("registry-add-label").value.trim();
  const stage = el("registry-add-stage").value;
  if (!engine || !modelId || !label) {
    print(status, "Enter engine, model id, and label", true);
    return;
  }
  const payload = { kind, engine, model_id: modelId, label, stage };
  if (kind === "llm") {
    payload.base_url = el("registry-add-base-url").value.trim();
    payload.api_key = el("registry-add-api-key").value.trim();
  }
  status.textContent = "Testing…";
  try {
    const resp = await fetch("/v1/model_registry", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await resp.json();
    if (!resp.ok) {
      print(status, body.detail || "Test failed", true);
      return;
    }
    status.textContent = `Added "${label}"`;
    el("registry-add-engine").value = "";
    el("registry-add-model-id").value = "";
    el("registry-add-label").value = "";
    await loadModelRegistry();
  } catch (error) {
    print(status, String(error), true);
  }
}

if (el("registry-add-kind")) el("registry-add-kind").addEventListener("change", _updateKindFields);
if (el("registry-add-btn")) el("registry-add-btn").addEventListener("click", createModelRegistryEntry);
if (el("model-registry-refresh")) el("model-registry-refresh").addEventListener("click", loadModelRegistry);
```

- [ ] **Step 2: Manually verify in the browser**

```bash
make start
```

Open `http://localhost:8000/static/index.html`, go to **Model Registry**, and confirm:
- Entries render as a `<table>` with columns Kind / Engine / Model / Label / Stage / (action).
- Select-all and per-row checkboxes work the same way as on Users (indeterminate state on partial selection).
- With 2+ entries selected, "Disable selected" dims those rows and flips their button to "Enable"; "Enable selected" reverses it; "Set stage: testing" / "Set stage: stable" updates the Stage `<select>` for every selected row.
- The per-row Stage select and Enable/Disable button still work unchanged.
- Adding a new entry via the existing form at the bottom still works (runs the live test, then appears in the table).

Then run `make stop`.

- [ ] **Step 3: Commit**

```bash
git add apps/api_gateway/app/static/js/model-registry.js
git commit -m "feat(ui): migrate Model Registry to shared data-table"
```

---

### Task 3: TTS Profiles page

**Files:**
- Modify: `apps/api_gateway/app/static/js/tts-profiles.js`

**Interfaces:**
- Consumes: `escapeHtml`, `runBulk`, `printBulkSummary` from `./helpers.js`; `renderDataTable` from `./data-table.js`.
- Produces: no change to any other exported function (`loadTtsProfiles`, `renderConvTtsProfileSelect`, `renderLivehostTtsProfileSelect`, `toggleTtsVoiceMode`, `loadTtsProfileVoiceOptions`, `openTtsProfileForm`, `resetTtsProfileForm`, `saveTtsProfile`, `deleteTtsProfile`, `cloneTtsProfile`) — `profiles.js` imports `renderProfileTtsSelect` from this file and is unaffected.

- [ ] **Step 1: Rewrite `renderTtsProfileList` and the delete path in `tts-profiles.js`**

Replace the full contents of `apps/api_gateway/app/static/js/tts-profiles.js` with:

```js
import { el, print, escapeHtml, runBulk, printBulkSummary, restoreAndBind } from "./helpers.js";
import { renderDataTable } from "./data-table.js";
import { renderProfileTtsSelect } from "./profiles.js";
import { fetchAuthStatus } from "./session.js";

export let ttsProfileData = {};
export let ttsProfileEditName = null; // null = "new" (no profile currently loaded into the form)

export async function loadTtsProfiles() {
  try {
    const body = await (await fetch("/v1/tts/profiles")).json();
    ttsProfileData = body.data || {};
    renderTtsProfileList();
    renderProfileTtsSelect();
    renderConvTtsProfileSelect();
    renderLivehostTtsProfileSelect();
  } catch {
    /* ignore */
  }
}

export function renderConvTtsProfileSelect() {
  const sel = el("conv-tts-profile");
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = '<option value="">(server default)</option>';
  Object.keys(ttsProfileData).sort().forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    sel.appendChild(opt);
  });
  if (ttsProfileData[prev]) sel.value = prev;
  restoreAndBind("conv-tts-profile");
}

export function renderLivehostTtsProfileSelect() {
  const sel = el("lh-tts-profile");
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = '<option value="">(server default)</option>';
  Object.keys(ttsProfileData).sort().forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    sel.appendChild(opt);
  });
  if (ttsProfileData[prev]) sel.value = prev;
  restoreAndBind("lh-tts-profile");
}

export async function renderTtsProfileList() {
  const host = el("tts-profile-list");
  if (!host) return;
  const names = Object.keys(ttsProfileData).sort();
  if (!names.length) {
    host.innerHTML = '<p class="hint">No TTS profiles yet. Create one below.</p>';
    return;
  }
  const status = await fetchAuthStatus();
  const isAdmin = !!(status && status.authenticated && status.role === "admin");

  const table = renderDataTable({
    container: host,
    rows: names,
    rowKey: (name) => name,
    emptyMessage: "No TTS profiles yet. Create one below.",
    columns: [
      {
        key: "name",
        label: "Name",
        render: (name) => {
          const p = ttsProfileData[name];
          const isTemplate = p.owner_id === null || p.owner_id === undefined;
          return `<strong>${escapeHtml(name)}</strong>${isTemplate ? "" : ' <span class="hint">mine</span>'}`;
        },
      },
      { key: "engine", label: "Engine", render: (name) => `<code>${escapeHtml(ttsProfileData[name].engine || "(no engine)")}</code>` },
      {
        key: "voice",
        label: "Voice",
        render: (name) => {
          const p = ttsProfileData[name];
          return escapeHtml(p.voice_mode === "clone" ? "cloned voice" : (p.voice || "auto voice"));
        },
      },
      {
        key: "actions",
        label: "",
        headerClass: "dt-actions-cell",
        cellClass: "dt-actions-cell",
        render: (name) => {
          const p = ttsProfileData[name];
          const isTemplate = p.owner_id === null || p.owner_id === undefined;
          const hideWriteControls = isTemplate && !isAdmin;
          return `
            ${hideWriteControls ? "" : `<button class="mini" data-tp-edit="${escapeHtml(name)}">Edit</button>`}
            <button class="mini" data-tp-clone="${escapeHtml(name)}">Clone</button>
            ${hideWriteControls ? "" : `<button class="mini danger" data-tp-delete="${escapeHtml(name)}">Delete</button>`}
          `;
        },
      },
    ],
    bulkActions: [
      { label: "Delete selected", run: (ids) => bulkDeleteTtsProfiles(ids) },
    ],
  });
  if (!table) return;

  table.querySelectorAll("[data-tp-edit]").forEach((btn) =>
    btn.addEventListener("click", () => openTtsProfileForm(btn.getAttribute("data-tp-edit")))
  );
  table.querySelectorAll("[data-tp-delete]").forEach((btn) =>
    btn.addEventListener("click", () => deleteTtsProfile(btn.getAttribute("data-tp-delete")))
  );
  table.querySelectorAll("[data-tp-clone]").forEach((btn) =>
    btn.addEventListener("click", () => cloneTtsProfile(btn.getAttribute("data-tp-clone")))
  );
}

export function toggleTtsVoiceMode() {
  const isClone = el("tp-mode-clone")?.checked;
  const presetWrap = el("tp-preset-wrap");
  const cloneWrap = el("tp-clone-wrap");
  if (presetWrap) presetWrap.classList.toggle("hidden", !!isClone);
  if (cloneWrap) cloneWrap.classList.toggle("hidden", !isClone);
}

export async function loadTtsProfileVoiceOptions(engine) {
  const sel = el("tp-voice");
  if (!sel) return;
  sel.innerHTML = '<option value="">(auto)</option>';
  if (!engine) return;
  try {
    const body = await (await fetch(`/v1/tts/voices?engine=${encodeURIComponent(engine)}`)).json();
    (body.data || []).forEach((v) => {
      const opt = document.createElement("option");
      opt.value = v.voice;
      opt.textContent = v.label;
      sel.appendChild(opt);
    });
  } catch {
    /* voices optional */
  }
}

export function openTtsProfileForm(name) {
  ttsProfileEditName = name || null;
  el("tp-form-title").textContent = name ? `Edit "${name}"` : "New TTS Profile";
  const p = name ? ttsProfileData[name] : null;

  el("tp-name").value = name || "";
  el("tp-name").disabled = !!name;
  el("tp-engine").value = p?.engine || "";
  const isClone = p?.voice_mode === "clone";
  el("tp-mode-preset").checked = !isClone;
  el("tp-mode-clone").checked = isClone;
  toggleTtsVoiceMode();
  loadTtsProfileVoiceOptions(p?.engine || "").then(() => {
    if (p?.voice) el("tp-voice").value = p.voice;
  });
  el("tp-ref-audio").value = p?.ref_audio_path || "";
  el("tp-ref-text").value = p?.ref_text || "";
  el("tp-instruct").value = p?.instruct || "";
  el("tp-speed").value = p?.speed ?? "";
  el("tp-language").value = p?.language || "";
  el("tp-delete-btn").classList.toggle("hidden", !name);
  el("tp-status").textContent = "";
}

export function resetTtsProfileForm() {
  openTtsProfileForm(null);
}

export async function saveTtsProfile() {
  const name = el("tp-name").value.trim();
  if (!name) { print(el("tp-status"), "Enter a profile name", true); return; }

  const speedRaw = el("tp-speed").value.trim();
  const payload = {
    name,
    engine: el("tp-engine").value || "",
    voice_mode: el("tp-mode-clone").checked ? "clone" : "preset",
    voice: el("tp-voice").value || "",
    ref_audio_path: el("tp-ref-audio").value.trim(),
    ref_text: el("tp-ref-text").value.trim(),
    instruct: el("tp-instruct").value.trim(),
    speed: speedRaw ? parseFloat(speedRaw) : null,
    language: el("tp-language").value.trim() || null,
  };

  print(el("tp-status"), "Saving…");
  try {
    const isNew = !ttsProfileEditName;
    const url = isNew ? "/v1/tts/profiles" : `/v1/tts/profiles/${encodeURIComponent(ttsProfileEditName)}`;
    const resp = await fetch(url, {
      method: isNew ? "POST" : "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await resp.json();
    if (!resp.ok) {
      print(el("tp-status"), body.detail || body.error || JSON.stringify(body), true);
      return;
    }
    el("tp-status").textContent = "Saved ✓";
    await loadTtsProfiles();
    openTtsProfileForm(name);
  } catch (error) {
    print(el("tp-status"), String(error), true);
  }
}

async function _deleteTtsProfileRaw(name) {
  try {
    const resp = await fetch(`/v1/tts/profiles/${encodeURIComponent(name)}`, { method: "DELETE" });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      return { ok: false, error: body.detail || "Delete failed" };
    }
    return { ok: true };
  } catch (error) {
    return { ok: false, error: String(error) };
  }
}

export async function deleteTtsProfile(name) {
  if (!confirm(`Delete TTS profile "${name}"?`)) return;
  const result = await _deleteTtsProfileRaw(name);
  if (!result.ok) {
    print(el("tp-status"), result.error, true);
    return;
  }
  await loadTtsProfiles();
  if (ttsProfileEditName === name) resetTtsProfileForm();
}

async function bulkDeleteTtsProfiles(names) {
  if (!confirm(`Delete ${names.length} TTS profile(s)?`)) return;
  const errors = await runBulk(names, _deleteTtsProfileRaw, (name) => name);
  await loadTtsProfiles();
  if (names.includes(ttsProfileEditName)) resetTtsProfileForm();
  printBulkSummary(el("tp-status"), names.length, errors, "Deleted");
}

export async function cloneTtsProfile(name) {
  const new_name = prompt(`Clone "${name}" as:`, `${name}-copy`);
  if (!new_name || !new_name.trim()) return;
  try {
    const resp = await fetch(`/v1/tts/profiles/${encodeURIComponent(name)}/clone`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ new_name: new_name.trim() }),
    });
    const body = await resp.json();
    if (!resp.ok) { print(el("tp-status"), body.detail || "Clone failed", true); return; }
    await loadTtsProfiles();
  } catch (error) {
    print(el("tp-status"), String(error), true);
  }
}

if (el("tp-engine")) {
  el("tp-engine").addEventListener("change", (e) => loadTtsProfileVoiceOptions(e.target.value));
}
if (el("tp-mode-preset")) el("tp-mode-preset").addEventListener("change", toggleTtsVoiceMode);
if (el("tp-mode-clone")) el("tp-mode-clone").addEventListener("change", toggleTtsVoiceMode);
if (el("tp-save-btn")) el("tp-save-btn").addEventListener("click", saveTtsProfile);
if (el("tp-cancel-btn")) el("tp-cancel-btn").addEventListener("click", resetTtsProfileForm);
if (el("tp-delete-btn")) el("tp-delete-btn").addEventListener("click", () => deleteTtsProfile(ttsProfileEditName));
```

- [ ] **Step 2: Manually verify in the browser**

```bash
make start
```

Open `http://localhost:8000/static/index.html`, go to **TTS Profiles**, and confirm:
- Profiles render as a `<table>` with columns Name / Engine / Voice / (actions).
- Edit/Clone/Delete buttons behave exactly as before (Edit opens the form below, Clone prompts for a new name, Delete asks to confirm).
- Selecting 2+ profiles and clicking "Delete selected" asks for one confirmation, deletes all of them, and reports the count; if the currently open edit form was for one of the deleted profiles, the form resets to "New TTS Profile".
- As a non-admin user, template profiles (no "mine" badge) hide their Edit/Delete buttons in the table exactly as before; Clone remains available.

Then run `make stop`.

- [ ] **Step 3: Commit**

```bash
git add apps/api_gateway/app/static/js/tts-profiles.js
git commit -m "feat(ui): migrate TTS Profiles to shared data-table"
```

---

### Task 4: MCP Servers page

**Files:**
- Modify: `apps/api_gateway/app/static/js/mcp-servers.js`

**Interfaces:**
- Consumes: `escapeHtml`, `runBulk`, `printBulkSummary` from `./helpers.js`; `renderDataTable` from `./data-table.js`.
- Produces: `mcpServerData`, `loadMcpServers`, `renderMcpList`, `toggleMcpServerEnabled`, `addMcpServer`, `testMcpServer`, `deleteMcpServer`, `cloneMcpServer` all keep their existing names and signatures — `profiles.js` imports `mcpServerData` from this file and is unaffected. The local `export function _escapeHtml` is removed; nothing else in the codebase imports it (confirmed by grep).

- [ ] **Step 1: Rewrite `mcp-servers.js`**

Replace the full contents of `apps/api_gateway/app/static/js/mcp-servers.js` with:

```js
import { el, print, escapeHtml, runBulk, printBulkSummary } from "./helpers.js";
import { renderDataTable } from "./data-table.js";
import { profileData, renderProfileMcpList } from "./profiles.js";
import { fetchAuthStatus } from "./session.js";

export let mcpServerData = {};     // loaded first so profile panel can use it

export async function loadMcpServers() {
  try {
    const body = await (await fetch("/v1/mcp/servers")).json();
    mcpServerData = body.data || {};
    renderMcpList();
  } catch {
    /* ignore */
  }
}

export async function renderMcpList() {
  const host = el("mcp-server-list");
  if (!host) return;
  const servers = Object.values(mcpServerData);
  if (!servers.length) {
    host.innerHTML = '<p class="hint">No servers configured yet. Add one below.</p>';
    return;
  }
  const status = await fetchAuthStatus();
  const isAdmin = !!(status && status.authenticated && status.role === "admin");

  const table = renderDataTable({
    container: host,
    rows: servers,
    rowKey: (s) => s.name,
    getRowClass: (s) => (s.enabled ? "" : "dim"),
    emptyMessage: "No servers configured yet. Add one below.",
    columns: [
      {
        key: "enabled",
        label: "On",
        render: (s) => {
          const isTemplate = s.owner_id === null || s.owner_id === undefined;
          const hideWriteControls = isTemplate && !isAdmin;
          return hideWriteControls
            ? ""
            : `<input type="checkbox" data-mcp-enabled="${escapeHtml(s.name)}" ${s.enabled ? "checked" : ""} title="Enabled" />`;
        },
      },
      {
        key: "name",
        label: "Name",
        render: (s) => {
          const isTemplate = s.owner_id === null || s.owner_id === undefined;
          return `<strong>${escapeHtml(s.name)}</strong>${isTemplate ? "" : ' <span class="hint">mine</span>'}`;
        },
      },
      {
        key: "url",
        label: "URL",
        render: (s) => `
          <code>${escapeHtml(s.url)}</code>
          ${s.headers && Object.keys(s.headers).length ? `<br /><span class="hint">headers: ${escapeHtml(Object.keys(s.headers).join(", "))}</span>` : ""}
        `,
      },
      {
        key: "tools",
        label: "Tools",
        render: (s) => `
          <span class="mcp-row-status" id="mcp-test-${escapeHtml(s.name)}"></span>
          <div class="mcp-tool-list" id="mcp-tools-${escapeHtml(s.name)}"></div>
        `,
      },
      {
        key: "actions",
        label: "",
        headerClass: "dt-actions-cell",
        cellClass: "dt-actions-cell",
        render: (s) => {
          const isTemplate = s.owner_id === null || s.owner_id === undefined;
          const hideWriteControls = isTemplate && !isAdmin;
          return `
            <button class="mini" data-mcp-test="${escapeHtml(s.name)}">Test</button>
            <button class="mini" data-mcp-clone="${escapeHtml(s.name)}">Clone</button>
            ${hideWriteControls ? "" : `<button class="mini danger" data-mcp-delete="${escapeHtml(s.name)}">Delete</button>`}
          `;
        },
      },
    ],
    bulkActions: [
      { label: "Enable selected", run: (ids) => bulkSetMcpEnabled(ids, true) },
      { label: "Disable selected", run: (ids) => bulkSetMcpEnabled(ids, false) },
      { label: "Delete selected", run: (ids) => bulkDeleteMcpServers(ids) },
    ],
  });
  if (!table) return;

  table.querySelectorAll("[data-mcp-enabled]").forEach((cb) =>
    cb.addEventListener("change", () =>
      toggleMcpServerEnabled(cb.getAttribute("data-mcp-enabled"), cb.checked)
    )
  );
  table.querySelectorAll("[data-mcp-test]").forEach((btn) =>
    btn.addEventListener("click", () => testMcpServer(btn.getAttribute("data-mcp-test")))
  );
  table.querySelectorAll("[data-mcp-delete]").forEach((btn) =>
    btn.addEventListener("click", () => deleteMcpServer(btn.getAttribute("data-mcp-delete")))
  );
  table.querySelectorAll("[data-mcp-clone]").forEach((btn) =>
    btn.addEventListener("click", () => cloneMcpServer(btn.getAttribute("data-mcp-clone")))
  );
}

async function _setMcpEnabledRaw(name, enabled) {
  try {
    const resp = await fetch(`/v1/mcp/servers/${encodeURIComponent(name)}/enabled`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      return { ok: false, error: body.detail || "Toggle failed" };
    }
    return { ok: true };
  } catch (error) {
    return { ok: false, error: String(error) };
  }
}

export async function toggleMcpServerEnabled(name, enabled) {
  const result = await _setMcpEnabledRaw(name, enabled);
  if (!result.ok) {
    print(el("mcp-status"), result.error, true);
    await loadMcpServers();
    return;
  }
  if (mcpServerData[name]) mcpServerData[name].enabled = enabled;
  renderMcpList();
}

async function bulkSetMcpEnabled(names, enabled) {
  const errors = await runBulk(names, (name) => _setMcpEnabledRaw(name, enabled), (name) => name);
  await loadMcpServers();
  printBulkSummary(el("mcp-status"), names.length, errors, enabled ? "Enabled" : "Disabled");
}

export async function addMcpServer() {
  const name = el("mcp-add-name").value.trim();
  const url = el("mcp-add-url").value.trim();
  const headerName = el("mcp-add-header-name").value.trim();
  const headerValue = el("mcp-add-header-value").value.trim();
  const status = el("mcp-status");
  if (!name || !url) { print(status, "Enter both name and URL", true); return; }
  const headers = headerName && headerValue ? { [headerName]: headerValue } : {};
  try {
    const resp = await fetch("/v1/mcp/servers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, url, headers }),
    });
    const body = await resp.json();
    if (!resp.ok) { print(status, body.detail || JSON.stringify(body), true); return; }
    status.textContent = `Added "${name}" ✓`;
    el("mcp-add-name").value = "";
    el("mcp-add-url").value = "";
    el("mcp-add-header-name").value = "";
    el("mcp-add-header-value").value = "";
    await loadMcpServers();
    // Refresh profile panel MCP list if open
    if (el("profile-panel") && !el("profile-panel").classList.contains("hidden")) {
      const pName = el("pf-name").value;
      renderProfileMcpList(pName && profileData[pName] ? profileData[pName].mcp_servers : []);
    }
  } catch (error) {
    print(status, String(error), true);
  }
}

export async function testMcpServer(name) {
  const statusEl = el(`mcp-test-${name}`);
  const listEl = el(`mcp-tools-${name}`);
  if (statusEl) { statusEl.textContent = "testing…"; statusEl.className = "mcp-row-status"; }
  if (listEl) listEl.innerHTML = "";
  try {
    const resp = await fetch(`/v1/mcp/servers/${encodeURIComponent(name)}/tools`);
    const body = await resp.json();
    if (!resp.ok) {
      if (statusEl) { statusEl.textContent = `✗ ${body.detail || "error"}`; statusEl.className = "mcp-row-status err"; }
      return;
    }
    const tools = body.data.tools || [];
    if (statusEl) { statusEl.textContent = `✓ ${tools.length} tool${tools.length !== 1 ? "s" : ""}`; statusEl.className = "mcp-row-status ok"; }
    if (listEl) {
      listEl.innerHTML = tools.map((t) => `
        <div class="mcp-tool-item">
          <code>${escapeHtml(t.name)}</code>
          <span>${escapeHtml(t.description || "")}</span>
        </div>
      `).join("");
    }
  } catch (error) {
    if (statusEl) { statusEl.textContent = `✗ ${error}`; statusEl.className = "mcp-row-status err"; }
  }
}

async function _deleteMcpServerRaw(name) {
  try {
    const resp = await fetch(`/v1/mcp/servers/${encodeURIComponent(name)}`, { method: "DELETE" });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      return { ok: false, error: body.detail || "Delete failed" };
    }
    return { ok: true };
  } catch (error) {
    return { ok: false, error: String(error) };
  }
}

export async function deleteMcpServer(name) {
  if (!confirm(`Delete MCP server "${name}"?`)) return;
  const result = await _deleteMcpServerRaw(name);
  if (!result.ok) {
    print(el("mcp-status"), result.error, true);
    return;
  }
  await loadMcpServers();
}

async function bulkDeleteMcpServers(names) {
  if (!confirm(`Delete ${names.length} MCP server(s)?`)) return;
  const errors = await runBulk(names, _deleteMcpServerRaw, (name) => name);
  await loadMcpServers();
  printBulkSummary(el("mcp-status"), names.length, errors, "Deleted");
}

export async function cloneMcpServer(name) {
  const new_name = prompt(`Clone "${name}" as:`, `${name}-copy`);
  if (!new_name || !new_name.trim()) return;
  try {
    const resp = await fetch(`/v1/mcp/servers/${encodeURIComponent(name)}/clone`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ new_name: new_name.trim() }),
    });
    const body = await resp.json();
    if (!resp.ok) { print(el("mcp-status"), body.detail || "Clone failed", true); return; }
    await loadMcpServers();
  } catch (error) {
    print(el("mcp-status"), String(error), true);
  }
}

if (el("mcp-add-btn")) el("mcp-add-btn").addEventListener("click", addMcpServer);
if (el("mcp-refresh")) el("mcp-refresh").addEventListener("click", loadMcpServers);
```

- [ ] **Step 2: Manually verify in the browser**

```bash
make start
```

Open `http://localhost:8000/static/index.html`, go to **MCP Servers**, and confirm:
- Servers render as a `<table>` with columns On / Name / URL / Tools / (actions).
- Clicking "Test" on a row's Test button still populates that same row's Tools column with the tool list (status text + tool items), in place, exactly as before.
- Selecting 2+ servers and clicking "Enable selected" / "Disable selected" toggles the On checkbox and row dimming for all of them; "Delete selected" asks for one confirmation and removes all selected servers, reporting any that failed (e.g., a template server your account isn't allowed to delete).
- The per-row On checkbox, Clone button, and (where visible) Delete button still work unchanged.

Then run `make stop`.

- [ ] **Step 3: Commit**

```bash
git add apps/api_gateway/app/static/js/mcp-servers.js
git commit -m "feat(ui): migrate MCP Servers to shared data-table"
```

---

### Task 5: Devices page

**Files:**
- Modify: `apps/api_gateway/app/static/js/devices.js`

**Interfaces:**
- Consumes: `escapeHtml`, `runBulk`, `printBulkSummary` from `./helpers.js`; `renderDataTable` from `./data-table.js`.
- Produces: `myDeviceData`, `allDeviceData`, `loadMyDevices`, `claimDevice` keep their existing names and signatures.

- [ ] **Step 1: Rewrite `devices.js`**

Replace the full contents of `apps/api_gateway/app/static/js/devices.js` with:

```js
import { el, print, escapeHtml, runBulk, printBulkSummary } from "./helpers.js";
import { renderDataTable } from "./data-table.js";
import { fetchAuthStatus } from "./session.js";

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
  if (!confirm("Revoke this device? It will need to be paired again.")) return;
  const result = await _revokeDeviceRaw(id, false);
  if (!result.ok) {
    print(el("device-status"), result.error, true);
    return;
  }
  await loadMyDevices();
}

async function revokeAnyDevice(id) {
  if (!confirm("Revoke this device? It will need to be paired again.")) return;
  const result = await _revokeDeviceRaw(id, true);
  if (!result.ok) {
    print(el("device-status"), result.error, true);
    return;
  }
  await maybeLoadAllDevices();
}

async function bulkRevokeDevices(ids, isAdminScope) {
  if (!confirm(`Revoke ${ids.length} device(s)? They will need to be paired again.`)) return;
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
```

- [ ] **Step 2: Manually verify in the browser**

```bash
make start
```

Open `http://localhost:8000/static/index.html`, go to **Devices**, and confirm:
- "My Devices" renders as a `<table>` with columns Name / Serial / Last seen / (action); as an admin, "All Devices" also renders as a table with an extra Owner column.
- Selecting 2+ devices and clicking "Revoke selected" shows one confirmation, then revokes all of them (rows go dim, Revoke button becomes disabled) and reports the count.
- An already-revoked device's row is dim and its Revoke button is disabled, in both the checkbox-selected and unselected state.
- The single-row Revoke button still works unchanged, in both "My Devices" and "All Devices".

Then run `make stop`.

- [ ] **Step 3: Commit**

```bash
git add apps/api_gateway/app/static/js/devices.js
git commit -m "feat(ui): migrate Devices to shared data-table"
```
