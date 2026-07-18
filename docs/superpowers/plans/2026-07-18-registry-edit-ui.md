# Registry Edit UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the Model Registry row's API Key / Base URL / Config behind a per-row Edit button that opens a detail row, and give Config a schema-driven Form mode alongside Raw JSON.

**Architecture:** A backend `config_schema` endpoint derives fields+types from the same `STT_ENGINE_CONFIG_DEFAULTS` / `OmnivoiceConfig` the resolvers use (no JS duplication). `data-table.js` gains an opt-in `rowDetail` hook. `model-registry.js` moves three columns into the detail row and builds the Form/Raw editor with type-correct save.

**Tech Stack:** Python 3.12, FastAPI, pytest (backend); vanilla ES modules (frontend, no test runner).

**Spec:** `docs/superpowers/specs/2026-07-18-registry-edit-ui-design.md`

## Global Constraints

- **Use `.venv/bin/python` for every python/pytest command.** Default `python` is pyenv 3.14, lacks ML deps. `.venv` is 3.12. Do NOT create a venv or `.python-version`.
- Run pytest backgrounded — `(cmd) & wait $!` — `tests/concurrency_guard.py` false-positives in the foreground.
- Baseline: `.venv/bin/python -m pytest -q` → **1 failed, 1026 passed** (the failure is `test_provider_single_flight_load.py::test_vieneu_provider_builds_model_once_under_race`, vieneu not installed, pre-existing). Any OTHER failure is yours.
- Type inference order: **`bool` before `int`** (bool is an int subclass in Python).
- The endpoint reads schemas, never a stored row, and never touches the DB.
- `data-table.js` change is **additive/opt-in**: with no `rowDetail`, existing tables are byte-for-byte unchanged in behavior.
- Frontend has no JS test runner — Tasks 2-3 verify by driving the running gateway UI and inspecting the PATCH body. Concrete steps are in each task.
- Config type coercion is mandatory: a number input yields `"1"` (string); it must reach the backend as `1` (int). `beam_size:"1"` would fail at transcribe time.

## File Structure

| File | Change |
|---|---|
| `apps/api_gateway/app/services/model_registry/config_schema.py` | NEW — pure function: `(kind, engine) → [{key,type,default}]` |
| `apps/api_gateway/app/api/routes/model_registry.py` | add `GET /config_schema` route |
| `tests/unit/test_model_registry_config_schema.py` | NEW — endpoint + function tests |
| `apps/api_gateway/app/static/js/data-table.js` | add opt-in `rowDetail` + fix checkbox index alignment |
| `apps/api_gateway/app/static/js/model-registry.js` | move 3 columns to detail row; Edit toggle; Form/Raw editor |
| `apps/api_gateway/app/static/styles.css` | detail-row + config-editor styles |

---

### Task 1: `config_schema` — backend field/type source

**Files:**
- Create: `apps/api_gateway/app/services/model_registry/config_schema.py`
- Modify: `apps/api_gateway/app/api/routes/model_registry.py` (add route after the GET "" handler, ~line 75)
- Test: `tests/unit/test_model_registry_config_schema.py`

**Interfaces:**
- Consumes: `STT_ENGINE_CONFIG_DEFAULTS` from `app.services.model_registry.resolve`; `OmnivoiceConfig` from `app.services.system_config`.
- Produces: `config_schema_for(kind: str, engine: str) -> list[dict]` where each dict is `{"key": str, "type": "bool"|"int"|"float"|"str", "default": <value>}`. And `GET /v1/model_registry/config_schema?kind=&engine=` → `{"fields": [...]}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_model_registry_config_schema.py
from app.services.model_registry.config_schema import config_schema_for


def _by_key(fields):
    return {f["key"]: f for f in fields}


def test_whisper_local_rich_schema_with_correct_types():
    fields = config_schema_for("stt", "whisper_local")
    by = _by_key(fields)
    assert by["default_model"] == {"key": "default_model", "type": "str", "default": "large-v3-turbo"}
    # bool must be reported as bool, not int (bool is an int subclass)
    assert by["vad_filter"]["type"] == "bool"
    assert by["beam_size"]["type"] == "int"
    assert by["condition_on_previous_text"]["type"] == "bool"


def test_remote_engines_expose_only_timeout_seconds():
    # config_schema_for keys off engine, not kind, for remote engines -- the
    # kind arg is irrelevant here, so pass a fixed one.
    for engine in ("openai_stt", "openai_tts", "whisper_service", "eventlab"):
        fields = config_schema_for("stt", engine)
        assert set(_by_key(fields)) == {"timeout_seconds"}
        assert _by_key(fields)["timeout_seconds"]["type"] == "float"


def test_omnivoice_exposes_its_config_fields():
    fields = config_schema_for("tts", "omnivoice")
    by = _by_key(fields)
    assert "omnivoice_model_id" in by and by["omnivoice_model_id"]["type"] == "str"
    assert by["omnivoice_use_server"]["type"] == "bool"
    assert by["omnivoice_server_port"]["type"] == "int"
    assert by["omnivoice_timeout_seconds"]["type"] == "float"


def test_unknown_and_llm_engines_have_no_fields():
    assert config_schema_for("llm", "openrouter") == []
    assert config_schema_for("tts", "edge_tts") == []
    assert config_schema_for("stt", "made_up") == []
```

- [ ] **Step 2: Run it, watch it fail**

Run: `(.venv/bin/python -m pytest tests/unit/test_model_registry_config_schema.py -q) & wait $!`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.model_registry.config_schema'`.

- [ ] **Step 3: Write the module**

```python
# apps/api_gateway/app/services/model_registry/config_schema.py
"""Describe a (kind, engine)'s config fields for the admin UI's Config form.

Single source of truth: the field lists come from the exact same
STT_ENGINE_CONFIG_DEFAULTS / OmnivoiceConfig the resolvers read, so the form can
never drift from what the backend actually honors. Types are inferred from each
default's Python type -- bool is checked before int because bool is an int
subclass. Engines with no known schema (llm, plain tts) return [] and the UI
falls back to raw-JSON editing.
"""

from __future__ import annotations

from app.services.model_registry.resolve import STT_ENGINE_CONFIG_DEFAULTS
from app.services.system_config import OmnivoiceConfig

# Remote STT/TTS providers read exactly one config key.
_REMOTE_ENGINES = {"openai_stt", "openai_tts", "whisper_service", "eventlab"}


def _type_of(value) -> str:
    if isinstance(value, bool):  # before int -- bool is a subclass of int
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return "str"


def _fields_from_defaults(defaults: dict) -> list[dict]:
    return [{"key": k, "type": _type_of(v), "default": v} for k, v in defaults.items()]


def config_schema_for(kind: str, engine: str) -> list[dict]:
    if engine in STT_ENGINE_CONFIG_DEFAULTS:
        return _fields_from_defaults(STT_ENGINE_CONFIG_DEFAULTS[engine])
    if engine in _REMOTE_ENGINES:
        return [{"key": "timeout_seconds", "type": "float", "default": 60.0}]
    if engine == "omnivoice":
        return _fields_from_defaults(OmnivoiceConfig().model_dump())
    return []
```

- [ ] **Step 4: Run the function tests**

Run: `(.venv/bin/python -m pytest tests/unit/test_model_registry_config_schema.py -q) & wait $!`
Expected: 4 passed.

- [ ] **Step 5: Add the route + an endpoint test**

In `apps/api_gateway/app/api/routes/model_registry.py`, add after the `list_entries` GET handler (~line 75):

```python
from app.services.model_registry.config_schema import config_schema_for


@router.get("/config_schema")
async def get_config_schema(kind: str, engine: str) -> dict:
    """Fields the Config form should render for this (kind, engine). Describes a
    schema, not a stored entry -- never reads the DB. Empty for engines with no
    known config shape (the UI falls back to raw JSON)."""
    return {"fields": config_schema_for(kind, engine)}
```

Add to the test file:

```python
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_endpoint_returns_fields(client):
    r = client.get("/v1/model_registry/config_schema", params={"kind": "stt", "engine": "whisper_local"})
    assert r.status_code == 200
    keys = {f["key"] for f in r.json()["fields"]}
    assert "beam_size" in keys and "vad_filter" in keys


def test_endpoint_empty_for_llm(client):
    r = client.get("/v1/model_registry/config_schema", params={"kind": "llm", "engine": "openrouter"})
    assert r.status_code == 200
    assert r.json() == {"fields": []}
```

Note: `/v1/model_registry/config_schema` is under the admin prefix; the `TestClient(app)` used here has no auth middleware blocking in the unit context the same way the other `model_registry` route tests run — mirror how `tests/unit/test_model_registry_routes.py` constructs its client and logs in if these endpoint tests return 401 (that file's `client` fixture + `_signup_login` admin helper is the pattern). If 401 appears, reuse that helper.

- [ ] **Step 6: Run + full suite**

Run: `(.venv/bin/python -m pytest tests/unit/test_model_registry_config_schema.py -q) & wait $!`
Expected: 6 passed.

Run: `(.venv/bin/python -m pytest -q) & wait $!`
Expected: 1 failed (baseline vieneu), rest passed.

- [ ] **Step 7: Commit**

```bash
git add apps/api_gateway/app/services/model_registry/config_schema.py apps/api_gateway/app/api/routes/model_registry.py tests/unit/test_model_registry_config_schema.py
git commit -m "feat(model-registry): config_schema endpoint for the Config form

Derives fields+types from STT_ENGINE_CONFIG_DEFAULTS / OmnivoiceConfig -- the
same values the resolvers read, so the UI form can't drift. Empty for engines
with no known shape (llm, plain tts); the UI falls back to raw JSON."
```

---

### Task 2: `data-table.js` — opt-in detail rows

**Files:**
- Modify: `apps/api_gateway/app/static/js/data-table.js`
- Modify: `apps/api_gateway/app/static/styles.css` (detail-row visibility)

**Interfaces:**
- Consumes: nothing.
- Produces: `renderDataTable` accepts an optional `rowDetail(row) -> string | null`. When it returns HTML for a row, a hidden `<tr class="dt-detail">` is rendered after that row's main `<tr>`. The returned `table` element exposes `table.toggleDetail(key)` to show/hide a row's detail by `rowKey`.

**The trap:** the current checkbox wiring (`data-table.js:76-98`) does `[...tbody.children].forEach((tr, i) => rowKey(rows[i]))` — it assumes `tbody.children[i]` is `rows[i]`. Interleaving detail `<tr>`s **breaks that index alignment**. The fix: tag main rows and iterate only those.

- [ ] **Step 1: Add `rowDetail` to the signature and render detail rows**

In `renderDataTable`'s destructured params, add `rowDetail`:

```javascript
export function renderDataTable({
  container,
  columns,
  rows,
  rowKey,
  getRowClass,
  bulkActions = [],
  emptyMessage = "No entries yet.",
  rowDetail = null,
}) {
```

Replace the `tbody.innerHTML = rows.map(...)` block with one that emits a main row (tagged `dt-main-row`, carrying its key) and, when `rowDetail` returns non-null, a hidden detail row carrying the same key:

```javascript
  const colCount = columns.length + 1; // + checkbox cell
  tbody.innerHTML = rows.map((row) => {
    const key = rowKey(row);
    const main = `
      <tr class="dt-main-row ${getRowClass ? getRowClass(row) : ""}" data-dt-key="${escapeHtml(String(key))}">
        <td class="dt-checkbox-cell"><input type="checkbox" /></td>
        ${columns.map((c) => `<td${c.cellClass ? ` class="${c.cellClass}"` : ""}>${c.render(row)}</td>`).join("")}
      </tr>`;
    if (!rowDetail) return main;
    const detail = rowDetail(row);
    if (detail == null) return main;
    return main + `
      <tr class="dt-detail" data-dt-detail-for="${escapeHtml(String(key))}" hidden>
        <td colspan="${colCount}">${detail}</td>
      </tr>`;
  }).join("");
```

- [ ] **Step 2: Fix the checkbox wiring to iterate only main rows**

Replace the two `[...tbody.children].forEach(...)` blocks (lines ~76-98) so they select main rows explicitly, keeping `rows[i]` alignment:

```javascript
  const mainRows = [...tbody.querySelectorAll("tr.dt-main-row")];

  mainRows.forEach((tr, i) => {
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
    mainRows.forEach((tr, i) => {
      const cb = tr.querySelector("input[type=checkbox]");
      cb.checked = checked;
      tr.classList.toggle("dt-row-selected", checked);
      if (checked) selected.add(rowKey(rows[i]));
    });
    updateSelectAllState();
    renderToolbar();
  });
```

`mainRows[i]` now aligns with `rows[i]` regardless of interleaved detail rows.

- [ ] **Step 3: Add the `toggleDetail` method before `return table`**

```javascript
  table.toggleDetail = (key) => {
    const detail = tbody.querySelector(`tr.dt-detail[data-dt-detail-for="${CSS.escape(String(key))}"]`);
    if (detail) detail.hidden = !detail.hidden;
    return detail ? !detail.hidden : false;
  };
```

- [ ] **Step 4: Style the detail row**

In `styles.css`, add:

```css
tr.dt-detail > td { padding: 0.75rem 1rem; background: rgba(255,255,255,0.02); }
tr.dt-detail[hidden] { display: none; }
```

- [ ] **Step 5: Verify no existing table regressed**

`data-table.js` is used by more than the registry (grep `renderDataTable`). Because `rowDetail` defaults to null and the only structural change is `mainRows` selection (which returns the same rows as before when no detail exists), existing tables are unaffected.

Run: `(cd /Users/lugon/code/speech-text-transformer && grep -rl "renderDataTable(" apps/api_gateway/app/static/js/)`
For each caller, open it in the running gateway UI (browser refresh — static files reload from disk, no restart) and confirm: rows render, checkboxes select, select-all works, bulk actions fire. Report which tables you checked.

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/static/js/data-table.js apps/api_gateway/app/static/styles.css
git commit -m "feat(data-table): opt-in rowDetail expand rows

renderDataTable takes rowDetail(row)->html; a non-null return renders a hidden
detail <tr> under the main row, toggled via table.toggleDetail(key). Checkbox
wiring now selects tr.dt-main-row explicitly so interleaved detail rows don't
break the rows[i] index alignment. No-op for tables that don't pass rowDetail."
```

---

### Task 3: `model-registry.js` — Edit expand + Form/Raw config

**Files:**
- Modify: `apps/api_gateway/app/static/js/model-registry.js`
- Modify: `apps/api_gateway/app/static/styles.css` (config-editor layout)

**Interfaces:**
- Consumes: `renderDataTable({rowDetail})` and `table.toggleDetail(key)` from Task 2; `GET /v1/model_registry/config_schema` from Task 1; existing `patchEntry(id, fields)`.
- Produces: no new exports; the registry table's row editor.

- [ ] **Step 1: Remove the three inline columns; add Edit to actions**

In `renderModelRegistry` (`model-registry.js:42-93`), delete the `api_key`, `base_url`, and `config` column objects. Change the `actions` column to render an Edit button beside the toggle:

```javascript
      {
        key: "actions",
        label: "",
        headerClass: "dt-actions-cell",
        cellClass: "dt-actions-cell",
        render: (e) => `
          <button class="mini" data-registry-edit="${escapeHtml(e.id)}">Edit</button>
          <button class="mini" data-registry-toggle="${escapeHtml(e.id)}">${e.enabled ? "Disable" : "Enable"}</button>
        `,
      },
```

Add `rowDetail` to the `renderDataTable(...)` call:

```javascript
    rowDetail: (e) => _detailHtml(e),
```

- [ ] **Step 2: Build the detail HTML**

Add `_detailHtml(e)` — the API Key / Base URL / Config editor. Config starts in Form mode with a placeholder the schema fetch fills in:

```javascript
function _detailHtml(e) {
  return `
    <div class="registry-detail" data-registry-detail="${escapeHtml(e.id)}">
      <label class="registry-field">
        <span>API Key</span>
        <code class="hint">${escapeHtml(e.api_key || "not set")}</code>
        <input type="password" class="mini" data-detail-apikey placeholder="new key…" autocomplete="off" />
      </label>
      <label class="registry-field">
        <span>Base URL</span>
        <input type="text" class="mini" data-detail-baseurl value="${escapeHtml(e.base_url || "")}" placeholder="https://…" />
      </label>
      <div class="registry-field">
        <span>Config</span>
        <div class="config-mode-toggle">
          <button type="button" class="mini" data-config-mode="form">Form</button>
          <button type="button" class="mini ghost" data-config-mode="raw">Raw JSON</button>
        </div>
        <div class="config-form" data-config-form>Loading fields…</div>
        <textarea class="mini config-raw" rows="4" data-config-raw hidden>${escapeHtml(JSON.stringify(e.config || {}, null, 2))}</textarea>
        <p class="config-error hint" data-config-error hidden></p>
      </div>
      <button class="mini" data-detail-save="${escapeHtml(e.id)}">Save</button>
    </div>`;
}
```

- [ ] **Step 3: Wire Edit toggle + lazy schema load**

After `renderDataTable` returns `table`, wire the Edit buttons. On first expand, fetch the schema and build the form. Store per-row state (schema, current mode) on a `Map` keyed by id.

```javascript
  const detailState = new Map(); // id -> { schema, mode }

  table.querySelectorAll("[data-registry-edit]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const id = btn.getAttribute("data-registry-edit");
      const nowOpen = table.toggleDetail(id);
      if (!nowOpen || detailState.has(id)) return;
      const entry = registryData.find((x) => x.id === id);
      const schema = await _fetchSchema(entry.kind, entry.engine);
      detailState.set(id, { schema, mode: "form" });
      _renderConfigForm(id, schema, entry.config || {});
    })
  );
```

```javascript
async function _fetchSchema(kind, engine) {
  try {
    const r = await fetch(`/v1/model_registry/config_schema?kind=${encodeURIComponent(kind)}&engine=${encodeURIComponent(engine)}`, { credentials: "same-origin" });
    if (!r.ok) return [];
    return (await r.json()).fields || [];
  } catch {
    return [];
  }
}
```

- [ ] **Step 4: Render the Form + coercion helpers**

```javascript
function _detailEl(id) {
  return document.querySelector(`[data-registry-detail="${CSS.escape(id)}"]`);
}

function _renderConfigForm(id, schema, config) {
  const host = _detailEl(id).querySelector("[data-config-form]");
  if (!schema.length) {
    host.innerHTML = `<p class="hint">No preset fields for this engine — use Raw JSON.</p>`;
    return;
  }
  host.innerHTML = schema.map((f) => {
    const val = config[f.key];
    if (f.type === "bool") {
      return `<label class="config-row"><input type="checkbox" data-cfg="${escapeHtml(f.key)}" ${val ? "checked" : ""}/> ${escapeHtml(f.key)}</label>`;
    }
    const inputType = (f.type === "int" || f.type === "float") ? "number" : "text";
    const v = val === undefined ? "" : String(val);
    return `<label class="config-row"><span>${escapeHtml(f.key)}</span>
      <input type="${inputType}" data-cfg="${escapeHtml(f.key)}" data-cfg-type="${f.type}"
             value="${escapeHtml(v)}" placeholder="${escapeHtml(String(f.default))}" /></label>`;
  }).join("");
}

// Gather the form into a typed config object. Throws on a bad number.
function _configFromForm(id) {
  const host = _detailEl(id).querySelector("[data-config-form]");
  const out = {};
  host.querySelectorAll("[data-cfg]").forEach((input) => {
    const key = input.getAttribute("data-cfg");
    if (input.type === "checkbox") { out[key] = input.checked; return; }
    const raw = input.value.trim();
    if (raw === "") return; // omit empty -> resolver falls back to default
    const t = input.getAttribute("data-cfg-type");
    if (t === "int" || t === "float") {
      const n = Number(raw);
      if (Number.isNaN(n)) throw new Error(`${key} must be a number`);
      out[key] = t === "int" ? Math.trunc(n) : n;
    } else {
      out[key] = raw;
    }
  });
  return out;
}
```

- [ ] **Step 5: Mode toggle with two-way sync**

```javascript
  table.querySelectorAll("[data-config-mode]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const detail = btn.closest("[data-registry-detail]");
      const id = detail.getAttribute("data-registry-detail");
      const mode = btn.getAttribute("data-config-mode");
      const form = detail.querySelector("[data-config-form]");
      const raw = detail.querySelector("[data-config-raw]");
      const err = detail.querySelector("[data-config-error]");
      err.hidden = true;
      if (mode === "raw") {
        // form -> raw: serialize (only if the form has fields loaded)
        try {
          const st = detailState.get(id);
          if (st && st.schema.length) raw.value = JSON.stringify(_configFromForm(id), null, 2);
        } catch (e) { err.textContent = e.message; err.hidden = false; return; }
        form.hidden = true; raw.hidden = false;
      } else {
        // raw -> form: parse back
        try {
          const parsed = JSON.parse(raw.value || "{}");
          const st = detailState.get(id);
          _renderConfigForm(id, st.schema, parsed);
        } catch { err.textContent = "Invalid JSON — fix it or stay in Raw mode"; err.hidden = false; return; }
        raw.hidden = true; form.hidden = false;
      }
      _setModeButtons(detail, mode);
    })
  );
```

Add a tiny `_setModeButtons(detail, mode)` that toggles the `ghost` class on the two mode buttons so the active one is highlighted.

- [ ] **Step 6: Save — visible mode is the source, with coercion**

```javascript
  table.querySelectorAll("[data-detail-save]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const id = btn.getAttribute("data-detail-save");
      const detail = _detailEl(id);
      const err = detail.querySelector("[data-config-error]");
      err.hidden = true;
      const rawVisible = !detail.querySelector("[data-config-raw]").hidden;
      let config;
      try {
        config = rawVisible
          ? JSON.parse(detail.querySelector("[data-config-raw]").value || "{}")
          : _configFromForm(id);
      } catch (e) { err.textContent = e.message || "Invalid config"; err.hidden = false; return; }

      const fields = { config };
      const apikey = detail.querySelector("[data-detail-apikey]").value.trim();
      if (apikey) fields.api_key = apikey; // blank = keep existing
      fields.base_url = detail.querySelector("[data-detail-baseurl]").value.trim();
      await patchEntry(id, fields);
    })
  );
```

- [ ] **Step 7: Style the editor**

In `styles.css`:

```css
.registry-detail { display: flex; flex-direction: column; gap: 0.6rem; max-width: 40rem; }
.registry-field { display: flex; flex-direction: column; gap: 0.25rem; }
.registry-field > span { font-size: 0.75rem; opacity: 0.7; }
.config-mode-toggle { display: flex; gap: 0.25rem; margin-bottom: 0.25rem; }
.config-row { display: flex; align-items: center; gap: 0.5rem; margin: 0.15rem 0; }
.config-row > span { min-width: 12rem; font-size: 0.8rem; }
.config-error { color: var(--danger, #e66); }
```

- [ ] **Step 8: Drive the UI to verify (no JS test runner)**

The change is served by the running gateway; static files reload on browser refresh (no restart). If the gateway isn't running the merged code, note it and skip live verification rather than testing stale code.

Verify, with the browser devtools Network tab open:
1. A `whisper_local` row → Edit expands the detail. Config shows a Form with `default_model`, `vad_filter` (checkbox), `beam_size` (number), etc.
2. Toggle `vad_filter` off, set `beam_size` to `2`, Save. In the Network tab, the PATCH body's `config` must have `"beam_size": 2` (number, not `"2"`) and `"vad_filter": false` (bool). **This is the type-coercion check — the whole point.**
3. Switch to Raw JSON: it shows the same config as JSON. Edit it, switch back to Form: the fields reflect the edit. Break the JSON, try to switch to Form: inline error, stays in Raw.
4. An `openrouter` (llm) row → Form shows "No preset fields — use Raw JSON"; Raw still saves.
5. Confirm the main table is now compact (no API Key/Base URL/Config columns) and Enable/Disable + bulk-select still work.

Paste the observed PATCH body for step 2 into the report — it is the proof.

- [ ] **Step 9: Commit**

```bash
git add apps/api_gateway/app/static/js/model-registry.js apps/api_gateway/app/static/styles.css
git commit -m "feat(model-registry): Edit expand row + Form/Raw config editor

Main row is compact; Edit opens a detail row with API Key / Base URL / Config.
Config has a schema-driven Form (from /config_schema) and Raw JSON, two-way
synced. Form coerces number->int/float and checkbox->bool before PATCH, so
beam_size reaches the backend as 1, not \"1\"."
```

---

## Verification

1. Backend suite: 1 failed (baseline vieneu) / rest passed; `config_schema` returns correct fields+types for whisper_local / openai_tts / omnivoice / llm.
2. UI (driven live): Edit expands; Form saves numbers as numbers and bools as bools (PATCH body confirmed); Form↔Raw round-trips; unknown engine falls back to Raw; existing tables using `data-table.js` still select/bulk-act correctly.
