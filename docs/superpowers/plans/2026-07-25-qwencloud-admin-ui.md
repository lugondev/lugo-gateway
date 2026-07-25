# QwenCloud Admin UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator create and configure a `qwencloud` STT Model Registry entry entirely through the admin UI — via both a linked "Qwen Cloud (DashScope)" provider and direct engine-dropdown selection — with a friendly config form (enum dropdowns for `realtime_model` / `turn_detection`).

**Architecture:** Small backend additions (a `qwencloud` config schema with an optional `choices` field-spec key; route helpers that mark `qwencloud` as a fixed-endpoint api-key-only service) plus frontend wiring in the vanilla admin JS (`model-registry.js`): engine inference, dropdown inclusion + base_url prefill, model suggestions, and `<select>` rendering for `choices` fields.

**Tech Stack:** Python 3.12 / FastAPI (pytest for backend), vanilla ES-module admin UI (`apps/api_gateway/app/static/js/model-registry.js`). Design spec: `docs/superpowers/specs/2026-07-25-qwencloud-admin-ui-design.md`.

## Global Constraints
- The `qwencloud` STT engine already exists and is registered (`stt_service.providers["qwencloud"]`, schema regex, `list_engines` row `mode:"remote"`). This plan adds NO engine behavior.
- Config field keys the engine reads (exact): `realtime_model`, `language`, `turn_detection`, `semantic_punctuation`, `timeout_seconds`. Defaults: `realtime_model="qwen3-asr-flash-realtime"`, `turn_detection="server_vad"`, `semantic_punctuation=false`, `timeout_seconds=60.0`, `language=""`.
- Enum choices: `realtime_model ∈ {"qwen3-asr-flash-realtime","fun-asr-realtime"}`; `turn_detection ∈ {"server_vad","manual"}`.
- Default base_url: `https://dashscope-intl.aliyuncs.com`.
- **Static-UI editing hazard (project memory):** this environment's Bash mangles non-ASCII / can silently corrupt smart quotes; `node --check` can false-pass. **Verify every JS edit by Reading the file back**, and never introduce non-ASCII characters into the JS. Keep all strings ASCII.
- Tests live in repo-root `tests/unit/`, import `from app...`, run from repo root with the shared venv. One pytest at a time (concurrency guard). No push/deploy.

## File Structure
- **Modify** `apps/api_gateway/app/services/model_registry/config_schema.py` — add a `qwencloud` STT branch returning fields with optional `choices`.
- **Modify** `apps/api_gateway/app/api/routes/model_registry.py` — mark `qwencloud` a fixed-endpoint service in `_location`/`_requires_base_url`.
- **Modify** `apps/api_gateway/app/static/js/model-registry.js` — `_effectiveEngine`, `_loadEngineOptions`, `_loadModelChoices`, `_renderConfigForm`.
- **Modify** `tests/unit/test_model_registry_config_schema.py` — qwencloud schema test.
- **Modify** `tests/unit/test_model_registry_options_route.py` (or `test_model_registry_routes.py`) — qwencloud `location`/`requires_base_url` in the engines list.

---

## Task 1: Backend — qwencloud config schema + fixed-endpoint service classification

**Files:**
- Modify: `apps/api_gateway/app/services/model_registry/config_schema.py`
- Modify: `apps/api_gateway/app/api/routes/model_registry.py:34-94`
- Test: `tests/unit/test_model_registry_config_schema.py`, `tests/unit/test_model_registry_options_route.py`

**Interfaces:**
- Produces: `config_schema_for("stt","qwencloud")` → list of `{key,type,default[,choices]}`; `_location("stt","qwencloud")=="service"`; `_requires_base_url("stt","qwencloud")==False`.

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_model_registry_config_schema.py`:
```python
def test_qwencloud_schema_has_enum_choices_and_defaults():
    fields = config_schema_for("stt", "qwencloud")
    by = _by_key(fields)
    assert set(by) == {"realtime_model", "language", "turn_detection",
                       "semantic_punctuation", "timeout_seconds"}
    assert by["realtime_model"]["default"] == "qwen3-asr-flash-realtime"
    assert by["realtime_model"]["choices"] == ["qwen3-asr-flash-realtime", "fun-asr-realtime"]
    assert by["turn_detection"]["choices"] == ["server_vad", "manual"]
    assert by["turn_detection"]["default"] == "server_vad"
    assert by["semantic_punctuation"]["type"] == "bool"
    assert by["timeout_seconds"]["type"] == "float"
    # non-enum fields carry no `choices` key
    assert "choices" not in by["language"]
```

Append to `tests/unit/test_model_registry_options_route.py` (a test that hits the stt engines/options list the UI reads — mirror an existing test in that file that asserts `requires_base_url`/`location`; if the assertion helpers differ, follow the file's existing style):
```python
def test_qwencloud_is_a_fixed_endpoint_service_engine():
    from app.api.routes.model_registry import _location, _requires_base_url
    assert _location("stt", "qwencloud") == "service"
    assert _requires_base_url("stt", "qwencloud") is False
```

- [ ] **Step 2: Run to confirm they fail**

Run: `cd /Users/lugon/code/speech-text-transformer && .venv/bin/python -m pytest tests/unit/test_model_registry_config_schema.py::test_qwencloud_schema_has_enum_choices_and_defaults tests/unit/test_model_registry_options_route.py::test_qwencloud_is_a_fixed_endpoint_service_engine -v`
Expected: FAIL — qwencloud returns `[]` from config_schema (falls through), and `_location` returns `"local"` / `_requires_base_url` `False`-for-the-wrong-reason (actually returns False because location is "local" — so assert `_location=="service"` fails first).

- [ ] **Step 3: Implement the config schema**

In `config_schema.py`, add a `qwencloud` branch in `config_schema_for` BEFORE the `_REMOTE_ENGINES` check (so it isn't swallowed) — note `qwencloud` is NOT added to `_REMOTE_ENGINES` (that set means "only timeout_seconds"):
```python
    if kind == "stt" and engine == "qwencloud":
        return [
            {"key": "realtime_model", "type": "str", "default": "qwen3-asr-flash-realtime",
             "choices": ["qwen3-asr-flash-realtime", "fun-asr-realtime"]},
            {"key": "language", "type": "str", "default": ""},
            {"key": "turn_detection", "type": "str", "default": "server_vad",
             "choices": ["server_vad", "manual"]},
            {"key": "semantic_punctuation", "type": "bool", "default": False},
            {"key": "timeout_seconds", "type": "float", "default": 60.0},
        ]
```
(Place it as the first check inside `config_schema_for`. Existing fields from `_fields_from_defaults` keep their exact 3-key shape — only these qwencloud enum fields carry `choices`.)

- [ ] **Step 4: Implement the service classification**

In `model_registry.py`, add a fixed-endpoint set and use it. After the existing `_OPENROUTER_STT_ENGINES = {"qwen3_asr_or", "whisper_or"}` (line 34), add:
```python
# STT engines that ARE services but hit a fixed vendor endpoint with a default
# base_url -- api_key only, no admin-supplied base_url required (like OpenRouter).
_FIXED_ENDPOINT_STT_ENGINES = _OPENROUTER_STT_ENGINES | {"qwencloud"}
```
Add `qwencloud` to the `_location` "service" test (extend the `or engine in ...` chain with `or engine == "qwencloud"`), and change `_requires_base_url`'s final line from:
```python
    return _location(kind, engine) == "service" and engine not in _OPENROUTER_STT_ENGINES
```
to:
```python
    return _location(kind, engine) == "service" and engine not in _FIXED_ENDPOINT_STT_ENGINES
```

- [ ] **Step 5: Run tests to confirm pass**

Run: `.venv/bin/python -m pytest tests/unit/test_model_registry_config_schema.py tests/unit/test_model_registry_options_route.py -v`
Expected: PASS (new + existing). The existing `test_remote_engines_expose_only_timeout_seconds` must still pass (qwencloud is not in that loop).

- [ ] **Step 6: Commit**
```bash
git add apps/api_gateway/app/services/model_registry/config_schema.py apps/api_gateway/app/api/routes/model_registry.py tests/unit/test_model_registry_config_schema.py tests/unit/test_model_registry_options_route.py
git commit -m "feat(admin): qwencloud config schema (enum choices) + fixed-endpoint classification"
```

---

## Task 2: Frontend — create-form: engine inference, dropdown, base_url prefill, model suggestions

**Files:**
- Modify: `apps/api_gateway/app/static/js/model-registry.js` (`_effectiveEngine` ~70, `_loadEngineOptions` ~42, `_loadModelChoices` ~93)

**Interfaces:**
- Consumes: Task 1's engine classification (so the base_url field reads as optional). No backend calls change shape.

- [ ] **Step 1: `_effectiveEngine` — DashScope provider → qwencloud**

In the `if (providerId)` branch, for STT, before the OpenRouter/http_stt return, add a DashScope check. Replace:
```javascript
    const base = (prov?.base_url || "").toLowerCase();
    return base.includes("openrouter.ai") ? "qwen3_asr_or" : "http_stt";
```
with:
```javascript
    const base = (prov?.base_url || "").toLowerCase();
    if (base.includes("dashscope")) return "qwencloud";
    return base.includes("openrouter.ai") ? "qwen3_asr_or" : "http_stt";
```

- [ ] **Step 2: `_loadEngineOptions` — include qwencloud in the dropdown + prefill base_url**

Change the local-only filter so qwencloud (remote) is selectable for STT:
```javascript
  for (const e of engines.filter((x) => x.mode === "local" || x.engine === "qwencloud")) {
```
And the previously-selected-value restore guard similarly:
```javascript
  if (engines.some((e) => e.engine === prev && (e.mode === "local" || e.engine === "qwencloud"))) sel.value = prev;
```
Then, so a direct (no-provider) qwencloud selection gets the default endpoint, prefill the base_url input when qwencloud is the current dropdown value and the field is empty. Add at the end of `_loadEngineOptions`, before `void _loadModelChoices();`:
```javascript
  const baseInput = el("registry-add-base-url");
  if (baseInput && sel.value === "qwencloud" && !baseInput.value.trim()) {
    baseInput.value = "https://dashscope-intl.aliyuncs.com";
  }
```

- [ ] **Step 3: `_loadModelChoices` — suggest qwencloud ASR models**

At the top of `_loadModelChoices`, after the existing `const kind = ...` lines, add a qwencloud short-circuit that seeds the two ASR models regardless of provider vs. no-provider (both create a qwencloud entry):
```javascript
    if (kind === "stt" && _effectiveEngine() === "qwencloud") {
      _modelChoices = ["qwen3-asr-flash", "fun-asr"];
    } else if (providerId) {
```
(i.e. turn the existing `if (providerId) {` into an `else if`, keeping its body unchanged, and likewise the following `else if (kind === "stt" || kind === "tts")` stays as-is after it. Preserve the existing auto-fill-single-choice logic below untouched.)

- [ ] **Step 4: Verify the edits by Reading the file**

Read the three edited regions of `apps/api_gateway/app/static/js/model-registry.js` back and confirm: no non-ASCII characters were introduced, brackets/quotes balance, and the `else if` chain in `_loadModelChoices` is well-formed (the original `if (providerId)` body and the `else if (kind === "stt" ...)` body are intact). Then `node --check apps/api_gateway/app/static/js/model-registry.js` as a secondary check (knowing it can false-pass smart-quote corruption — the Read is the primary gate).

- [ ] **Step 5: Commit**
```bash
git add apps/api_gateway/app/static/js/model-registry.js
git commit -m "feat(admin-ui): create qwencloud entries via provider or engine dropdown"
```

---

## Task 3: Frontend — config form renders `<select>` for enum (`choices`) fields

**Files:**
- Modify: `apps/api_gateway/app/static/js/model-registry.js` (`_renderConfigForm` ~463)

**Interfaces:**
- Consumes: Task 1's schema (fields may carry `choices`). `_configFromForm` needs NO change — a `<select>` carrying `data-cfg` + `data-cfg-type="str"` is read by the existing non-checkbox branch via `.value`.

- [ ] **Step 1: Render `choices` as a `<select>`**

In `_renderConfigForm`, inside the `schema.map`, handle `choices` before the bool/text branches:
```javascript
  host.innerHTML = schema.map((f) => {
    const val = config[f.key];
    if (Array.isArray(f.choices)) {
      const cur = val === undefined ? f.default : val;
      const opts = f.choices.map((c) =>
        `<option value="${escapeHtml(String(c))}" ${String(c) === String(cur) ? "selected" : ""}>${escapeHtml(String(c))}</option>`
      ).join("");
      return `<label class="config-row"><span>${escapeHtml(f.key)}</span>
        <select data-cfg="${escapeHtml(f.key)}" data-cfg-type="str">${opts}</select></label>`;
    }
    if (f.type === "bool") {
```
(Leave the rest of the function — the bool checkbox and the text/number input — unchanged.)

- [ ] **Step 2: Verify the edit by Reading the file**

Read `_renderConfigForm` back; confirm ASCII-only, balanced template literals, and that the `<select>` carries `data-cfg` + `data-cfg-type="str"` (so `_configFromForm`'s existing else-branch reads it as a string). Confirm `_configFromForm` is unchanged. `node --check` as secondary.

- [ ] **Step 3: Backend contract sanity (no JS test harness exists)**

Since there is no JS test runner for this vanilla UI, assert the data contract the JS relies on with a quick backend check: run
`.venv/bin/python -c "from app.services.model_registry.config_schema import config_schema_for; import json; print(json.dumps(config_schema_for('stt','qwencloud')))"`
and confirm the two enum fields carry `choices` arrays (this is what `_renderConfigForm` keys off). Paste the output into the task report.

- [ ] **Step 4: Commit**
```bash
git add apps/api_gateway/app/static/js/model-registry.js
git commit -m "feat(admin-ui): enum dropdowns in the registry config form"
```

---

## Task 4: Regression gate

**Files:** none (verification).

- [ ] **Step 1: Run the model-registry backend suite**

Run: `.venv/bin/python -m pytest tests/unit/test_model_registry_config_schema.py tests/unit/test_model_registry_options_route.py tests/unit/test_model_registry_routes.py tests/unit/test_model_registry_provider_link.py tests/unit/test_qwencloud_stt_provider.py -v`
Expected: all PASS. Record the count in the report. If any sibling breaks (e.g. an options-route test that enumerates engines), investigate before proceeding.

- [ ] **Step 2: Commit (only if a fix was needed; otherwise skip)**

---

## Notes for the implementer
- Do NOT touch `qwencloud_provider.py` or any engine behavior — this is UI/registry-metadata only.
- Keep all JS ASCII. Prefer straight quotes. Re-Read every JS edit (the environment can silently corrupt non-ASCII; `node --check` is not sufficient).
- The two creation paths after this: (a) pick the "Qwen Cloud (DashScope)" provider → qwencloud entry; (b) select `qwencloud` in the engine dropdown (base_url prefilled, enter api_key). Config (esp. `realtime_model` for fun-asr) is set post-create in Edit → Config, now with dropdowns.
