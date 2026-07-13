# Admin UI: shared data-table component + bulk actions

## Problem

The admin UI's list-style pages (Users, Model Registry, Profiles, TTS Profiles,
MCP Servers, Devices) all render entities as a flex `.model-row` card list
(`apps/api_gateway/app/static/js/*.js` + `.model-row`/`.model-list` in
`styles.css`). There is no real `<table>` markup, no multi-select, and no bulk
actions anywhere in the app (verified by grep across `static/`). This makes
scanning/managing many rows (e.g. many users or model entries) awkward — every
action is one row, one click.

Goal: replace the card-list pattern with a real, dense `<table>` and
checkbox-based multi-select + bulk actions, applied consistently across all
six admin list pages.

## Non-goals

- No pagination, sorting, or search/filter — current row counts are small
  (tens, not thousands); add only if it becomes a real pain later.
- No new backend bulk endpoints. Bulk actions loop client-side over the
  existing per-item PATCH/DELETE/POST endpoints.
- No new delete capability for Users or Model Registry (backend has none —
  see below). Bulk "remove" on those two pages means disable, not delete.
- No frontend framework (React/Vue/Alpine). Stays vanilla ES modules,
  consistent with the rest of the codebase.

## Current backend capabilities (confirmed by reading routes)

| Entity | File | Verbs available | Relevant fields |
|---|---|---|---|
| Users | `api/routes/users.py` | GET, POST, PATCH | `disabled`, `role`, `can_use_testing` (no DELETE) |
| Model Registry | `api/routes/model_registry.py` | GET, POST, PATCH | `enabled`, `stage` (no DELETE) |
| Profiles | `api/routes/profiles.py` | GET, POST, PUT, DELETE, clone | — |
| TTS Profiles | `api/routes/tts_profiles.py` | GET, POST, PUT, DELETE, clone | — |
| MCP Servers | `api/routes/mcp.py` | GET, POST, PUT, PATCH `/enabled`, DELETE, clone | — |
| Devices | `api/routes/devices.py` | GET, POST `/revoke` | no delete, only revoke |

This table drives what each page's bulk toolbar offers (see below). No
backend changes are needed — everything bulk-related is a client-side loop
over these existing per-item endpoints.

## Architecture

### New shared module: `apps/api_gateway/app/static/js/data-table.js`

Exports a single function:

```js
renderDataTable({
  container,       // element to render into
  columns,         // [{ key, label, render(row) -> string|Node, className? }]
  rows,            // array of row objects
  rowKey,          // (row) => string, unique id
  getRowClass,     // (row) => string | undefined, e.g. "dim" for disabled rows
  bulkActions,     // [{ label, run(selectedIds, selectedRows) }]
  emptyMessage,    // string shown when rows.length === 0
})
```

Behavior:
- Renders a real `<table class="data-table">` with `<thead>`/`<tbody>`.
- First `<th>`/`<td>` in every row is a checkbox; header checkbox is
  "select all visible rows" (tri-state: checked/indeterminate/unchecked).
- Maintains its own selection `Set` internally, keyed by `rowKey`.
- When selection is non-empty, shows a `.dt-toolbar` bar above the table:
  "`N selected`" + one button per `bulkActions` entry. Toolbar disappears
  when selection is empty.
- Each bulk action button calls `run(selectedIds, selectedRows)`; the caller
  is responsible for looping over IDs, calling existing per-item endpoints,
  handling partial failures (collect errors, show a summary), and
  re-loading + re-rendering the table afterward. `data-table.js` itself does
  not know about fetch/API — it only renders and reports selection.
- Column `render` can return either a string (innerHTML) or a DOM node, so
  existing per-row controls (role `<select>`, "Testing" checkbox, mini
  action buttons) keep working as table cells instead of flex children.

This also absorbs the currently-duplicated `_escapeHtml()` (from `users.js`
and `model-registry.js`) into `helpers.js` as a shared export, used by
`data-table.js` and callers.

### Per-page integration

Each of the six page modules (`users.js`, `model-registry.js`, `profiles.js`,
`tts-profiles.js`, `mcp.js`, `devices.js`) keeps its existing `load*()` /
fetch / update logic, but its `render*()` function is rewritten to build a
`columns` array and call `renderDataTable(...)` instead of joining
`.model-row` HTML strings. Existing per-row interactive elements (role
select, testing checkbox, enable/disable button, reset-password button,
delete/clone buttons) become column cells.

Bulk actions per page (from the capability table above):

- **Users**: `Disable selected` / `Enable selected` (loops `PATCH
  disabled`), `Set role → admin/user` (loops `PATCH role`). Reuses the
  existing "last active admin" safeguard server-side — bulk demote/disable
  simply surfaces per-item 400 errors in the summary if the guard trips.
- **Model Registry**: `Enable selected` / `Disable selected` (loops `PATCH
  enabled`), `Set stage → stable/beta/...` (loops `PATCH stage`).
- **Profiles**: `Delete selected` (loops `DELETE`).
- **TTS Profiles**: `Delete selected` (loops `DELETE`).
- **MCP Servers**: `Delete selected`, `Enable selected` / `Disable selected`.
- **Devices**: `Revoke selected` (loops `POST /revoke`).

### Styling

New CSS in `styles.css`, following the existing design tokens (`--bg-1/2`,
`--card`, `--line`, `--accent`, `--r-*` radii, `--sp-*` spacing, Chakra
Petch / IBM Plex Mono fonts) rather than introducing new colors or a new
visual language:

- `.data-table` — full-width table, `border-collapse: collapse`, row
  dividers using `--line`, row hover using the same accent-tinted
  highlight currently on `.model-row:hover`.
- `.data-table tr.dim` — same 0.4 opacity treatment as today's disabled
  rows.
- `.dt-toolbar` — a slim bar (pill-ish, `--r-md`, accent-tinted background)
  that slides/fades in above the table when selection is non-empty.
- Checkbox cells reuse the existing `input[type="checkbox"]` styling
  already defined in `styles.css` (line ~507), just placed in a `<td>`.

Concrete visual details (spacing, hover states, toolbar placement) get
finalized during implementation using the `frontend-design` skill so the
table reads as intentional rather than a bare default `<table>`.

### Rollout order

One page per implementation task, in this order, so `data-table.js` is
proven on the two most-requested pages first and issues surface early:

1. `data-table.js` + shared `_escapeHtml` extraction (foundation, no visible
   change yet)
2. Users page
3. Model Registry page
4. Profiles page
5. TTS Profiles page
6. MCP Servers page
7. Devices page

Each task is independently testable (existing manual/browser check per
page) and independently reviewable.

## Testing

This is a UI-only change (no new backend endpoints), so there's no new
automated test surface beyond what already exists. Verification is manual,
per page, in the browser:

- Table renders all rows with correct data.
- Header checkbox selects/deselects all visible rows (and shows
  indeterminate state when some but not all are selected).
- Selecting rows shows the bulk toolbar with the right actions for that
  page; deselecting all hides it.
- Each bulk action performs the expected per-item API calls and the table
  refreshes afterward; a deliberately-triggered failure (e.g. bulk-disable
  the last admin) surfaces a clear error without silently succeeding.
- Existing per-row controls (role select, testing checkbox, reset password,
  clone, single delete/enable) still work unchanged.
