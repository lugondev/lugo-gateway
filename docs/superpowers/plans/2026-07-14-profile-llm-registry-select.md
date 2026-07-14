# Profile LLM Registry Select Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the Profile Configuration panel's LLM section pick a model from the admin-managed Model Registry instead of only free-text entry, while keeping a "Custom" fallback for the existing free-text fields.

**Architecture:** A new `GET /v1/profiles/llm-options` endpoint (under the already user-accessible `/v1/profiles` prefix, not the admin-only `/v1/model_registry` prefix) lists registry `kind="llm"` entries usable by the current user (enabled, and testing-stage only if `can_use_testing`). The frontend adds a `#pf-llm-select` dropdown (mirroring the existing `renderProfileTtsSelect` pattern) with a trailing "Custom…" option; picking a registry entry sets `llm.engine`/`llm.model` and clears `llm.base_url`/`llm.api_key` so the existing (already-built) `resolve_llm_override_from_registry()` supplies them at call time. Picking "Custom" preserves today's exact behavior.

**Tech Stack:** FastAPI (Python) backend, vanilla JS + HTML frontend (ES modules), pytest + `fastapi.testclient.TestClient`.

## Global Constraints

- No DB schema changes — `LlmConfig.engine`/`LlmConfig.model` already exist on `Profile` (`apps/api_gateway/app/services/profiles/models.py:8-12`).
- The new endpoint must NOT be added under the `/v1/model_registry` prefix — `AuthGuardMiddleware` (`apps/api_gateway/app/core/auth_guard.py:28`) gates that whole prefix to admins only, by string-prefix match, with no per-route exception mechanism.
- The new endpoint must strip `api_key`/`base_url` entirely from its response (not just mask) — regular users must never receive even a masked admin secret.
- Existing custom (free-text) profiles must keep working unchanged after this change (no forced migration).

---

### Task 1: Backend — `GET /v1/profiles/llm-options` endpoint

**Files:**
- Modify: `apps/api_gateway/app/api/routes/profiles.py` (add function + route between `create_profile` and `get_profile`, i.e. before the `/{name}` route so the literal path isn't swallowed by that path param)
- Test: `tests/unit/test_profiles_routes.py`

**Interfaces:**
- Consumes: `model_registry_store.list_all()` (`apps/api_gateway/app/services/model_registry/store.py:55-57`, returns `list[dict]` with keys `id, kind, engine, model_id, label, enabled, stage, api_key, base_url`); `_resolve_acting_user(request)` (`profiles.py:43-49`, returns `User | None`, already defined in this file).
- Produces: `GET /v1/profiles/llm-options` → `{"success": true, "data": [{"id": str, "engine": str, "model_id": str, "label": str}, ...]}`. Later tasks (frontend) consume this shape directly.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_profiles_routes.py` (check the file's existing imports first — it already imports `TestClient`/`app`/`client` fixture per the established pattern; add these using whatever local login helper that file already has, or inline signup+login if it has no helper):

```python
import asyncio

from app.services.model_registry.store import ModelRegistryStore
from app.services.auth.users import user_store


def test_llm_options_lists_enabled_stable_entries_for_regular_user(client, _with_password):
    store = ModelRegistryStore()
    asyncio.run(store.create("llm", "openai", "gpt-4o-mini", "GPT-4o mini", stage="stable"))
    _signup_login(client, "toan", role="user")
    resp = client.get("/v1/profiles/llm-options")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert any(e["engine"] == "openai" and e["model_id"] == "gpt-4o-mini" for e in data)
    entry = next(e for e in data if e["engine"] == "openai")
    assert "api_key" not in entry
    assert "base_url" not in entry


def test_llm_options_hides_testing_stage_for_non_tester(client, _with_password):
    store = ModelRegistryStore()
    asyncio.run(store.create("llm", "openrouter", "qwen3-instruct", "Qwen3 Instruct", stage="testing"))
    _signup_login(client, "toan2", role="user")
    resp = client.get("/v1/profiles/llm-options")
    assert resp.status_code == 200
    assert not any(e["engine"] == "openrouter" for e in resp.json()["data"])


def test_llm_options_shows_testing_stage_for_tester(client, _with_password):
    store = ModelRegistryStore()
    asyncio.run(store.create("llm", "openrouter", "qwen3-instruct-2", "Qwen3 Instruct 2", stage="testing"))
    _signup_login(client, "toan3", role="user")
    user = asyncio.run(user_store.get_by_username("toan3"))
    asyncio.run(user_store.set_fields(user.id, can_use_testing=True))
    resp = client.get("/v1/profiles/llm-options")
    assert resp.status_code == 200
    assert any(e["engine"] == "openrouter" and e["model_id"] == "qwen3-instruct-2" for e in resp.json()["data"])


def test_llm_options_hides_disabled_entries(client, _with_password):
    store = ModelRegistryStore()
    entry = asyncio.run(store.create("llm", "openai", "gpt-disabled", "Disabled model", stage="stable"))
    asyncio.run(store.set_fields(entry["id"], enabled=False))
    _signup_login(client, "toan4", role="user")
    resp = client.get("/v1/profiles/llm-options")
    assert resp.status_code == 200
    assert not any(e["engine"] == "openai" and e["model_id"] == "gpt-disabled" for e in resp.json()["data"])
```

If `test_profiles_routes.py` does not already define `_signup_login`, copy the exact helper from `tests/unit/test_model_registry_routes.py:25-34`:

```python
def _signup_login(client, username: str, role: str = "user") -> None:
    client.post("/api/auth/signup", json={"username": username, "password": "pw"})
    if role == "admin":
        import asyncio

        from app.services.auth.users import user_store

        user = asyncio.run(user_store.get_by_username(username))
        asyncio.run(user_store.set_fields(user.id, role="admin"))
    client.post("/api/auth/login", json={"username": username, "password": "pw"})
```

and the `_with_password` fixture from the same file (lines 18-22):

```python
@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")
```

(only add these two if they aren't already present in `test_profiles_routes.py` — check the file first; do not duplicate a fixture/function name that already exists there).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/api_gateway && python -m pytest tests/unit/test_profiles_routes.py -k llm_options -v`
Expected: FAIL — `404 Not Found` for `GET /v1/profiles/llm-options` (route doesn't exist yet), or `ImportError`/`NameError` if `ModelRegistryStore`/`_signup_login` aren't wired up yet.

- [ ] **Step 3: Implement the endpoint**

In `apps/api_gateway/app/api/routes/profiles.py`, add this import alongside the existing ones at the top of the file:

```python
from app.services.model_registry.store import model_registry_store
```

Then insert this route between `create_profile` (ends at line 100) and `get_profile` (`@router.get("/{name}")` at line 103) — literal-path routes must be registered before the `/{name}` path-param route or FastAPI will match `/{name}` first:

```python
@router.get("/llm-options")
async def list_llm_options(request: Request) -> dict:
    acting_user = await _resolve_acting_user(request)
    can_use_testing = bool(acting_user and acting_user.can_use_testing)
    entries = await model_registry_store.list_all()
    options = [
        {"id": e["id"], "engine": e["engine"], "model_id": e["model_id"], "label": e["label"]}
        for e in entries
        if e["kind"] == "llm" and e["enabled"] and (e["stage"] != "testing" or can_use_testing)
    ]
    return {"success": True, "data": options}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/api_gateway && python -m pytest tests/unit/test_profiles_routes.py -k llm_options -v`
Expected: 4 passed

- [ ] **Step 5: Run the full profiles test file to check for regressions**

Run: `cd apps/api_gateway && python -m pytest tests/unit/test_profiles_routes.py -v`
Expected: all passed (no pre-existing test broken by the new import/route)

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/api/routes/profiles.py tests/unit/test_profiles_routes.py
git commit -m "feat(profiles): add GET /v1/profiles/llm-options for registry-backed LLM picker"
```

---

### Task 2: Frontend — LLM select dropdown with Custom fallback

**Files:**
- Modify: `apps/api_gateway/app/static/index.html` (lines 189-201, the LLM Base URL/Model/API Key block)
- Modify: `apps/api_gateway/app/static/js/profiles.js`

**Interfaces:**
- Consumes: `GET /v1/profiles/llm-options` → `{"success": true, "data": [{"id": str, "engine": str, "model_id": str, "label": str}, ...]}` (Task 1).
- Produces: `llmOptionData` (module-level `let llmOptionData = []`, exported), `renderProfileLlmSelect()` (exported), `toggleLlmCustomFields()` (module-local, called on select change and on panel open). `saveProfile()`'s built `llm` payload object shape stays `{base_url, api_key, model, engine}` (matches `LlmConfig` in `apps/api_gateway/app/services/profiles/models.py:8-12`).

- [ ] **Step 1: Update the HTML structure**

In `apps/api_gateway/app/static/index.html`, replace lines 190-201:

```html
                  <label>
                    LLM Base URL
                    <input id="pf-llm-url" type="text" placeholder="https://api.openai.com/v1" />
                  </label>
                  <label>
                    LLM Model
                    <input id="pf-llm-model" type="text" placeholder="gpt-4o-mini" />
                  </label>
                  <label>
                    LLM API Key
                    <input id="pf-llm-key" type="password" placeholder="sk-&#8230; (leave blank to keep existing)" autocomplete="off" />
                  </label>
```

with:

```html
                  <label>
                    LLM
                    <select id="pf-llm-select">
                      <option value="__custom__">— Custom… —</option>
                    </select>
                  </label>
                  <div id="pf-llm-custom-fields">
                    <label>
                      LLM Base URL
                      <input id="pf-llm-url" type="text" placeholder="https://api.openai.com/v1" />
                    </label>
                    <label>
                      LLM Model
                      <input id="pf-llm-model" type="text" placeholder="gpt-4o-mini" />
                    </label>
                    <label>
                      LLM API Key
                      <input id="pf-llm-key" type="password" placeholder="sk-&#8230; (leave blank to keep existing)" autocomplete="off" />
                    </label>
                  </div>
```

- [ ] **Step 2: Add `llmOptionData` state and `renderProfileLlmSelect()`**

In `apps/api_gateway/app/static/js/profiles.js`, add after the `profileEditMode` export (after line 9):

```javascript
export let llmOptionData = [];

export async function loadLlmOptions() {
  try {
    const body = await (await fetch("/v1/profiles/llm-options")).json();
    llmOptionData = body.data || [];
  } catch {
    llmOptionData = [];
  }
}
```

Add after `renderProfileTtsSelect()` (after line 63):

```javascript
export function renderProfileLlmSelect() {
  const sel = el("pf-llm-select");
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = "";
  llmOptionData.forEach((entry) => {
    const opt = document.createElement("option");
    opt.value = entry.id;
    opt.textContent = `${entry.label} (${entry.engine}/${entry.model_id})`;
    sel.appendChild(opt);
  });
  const customOpt = document.createElement("option");
  customOpt.value = "__custom__";
  customOpt.textContent = "— Custom… —";
  sel.appendChild(customOpt);
  if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
}

export function toggleLlmCustomFields() {
  const sel = el("pf-llm-select");
  const fields = el("pf-llm-custom-fields");
  if (!sel || !fields) return;
  fields.classList.toggle("hidden", sel.value !== "__custom__");
}
```

- [ ] **Step 3: Wire `loadLlmOptions()`/`renderProfileLlmSelect()` into panel open, and select the right option for an existing profile**

In `openProfilePanel()`, replace this line (line 71):

```javascript
  renderProfileTtsSelect();
```

with:

```javascript
  renderProfileTtsSelect();
  await loadLlmOptions();
  renderProfileLlmSelect();
```

(this makes `openProfilePanel` need `async` before the loop body already runs — it's already declared `async function` at line 65, no change needed there since it's already `export async function openProfilePanel`.)

Replace the "new" branch's LLM reset (lines 79-81):

```javascript
    el("pf-llm-url").value = "";
    el("pf-llm-model").value = "";
    el("pf-llm-key").value = "";
```

with:

```javascript
    el("pf-llm-select").value = "__custom__";
    el("pf-llm-url").value = "";
    el("pf-llm-model").value = "";
    el("pf-llm-key").value = "";
    toggleLlmCustomFields();
```

Replace the "edit" branch's LLM population (lines 99-101):

```javascript
    el("pf-llm-url").value = p.llm?.base_url || "";
    el("pf-llm-model").value = p.llm?.model || "";
    el("pf-llm-key").value = "";
```

with:

```javascript
    const matchedOption = llmOptionData.find(
      (o) => o.engine === p.llm?.engine && o.model_id === p.llm?.model
    );
    el("pf-llm-select").value = matchedOption ? matchedOption.id : "__custom__";
    el("pf-llm-url").value = p.llm?.base_url || "";
    el("pf-llm-model").value = p.llm?.model || "";
    el("pf-llm-key").value = "";
    toggleLlmCustomFields();
```

- [ ] **Step 4: Update `saveProfile()` to send `engine` and clear custom fields when a registry entry is selected**

Replace the `llm` block in the payload (lines 167-171):

```javascript
    llm: {
      base_url: el("pf-llm-url").value.trim(),
      api_key: el("pf-llm-key").value,
      model: el("pf-llm-model").value.trim(),
    },
```

with:

```javascript
    llm: (() => {
      const selectedId = el("pf-llm-select")?.value;
      const selected = llmOptionData.find((o) => o.id === selectedId);
      if (selected) {
        return { base_url: "", api_key: "", model: selected.model_id, engine: selected.engine };
      }
      return {
        base_url: el("pf-llm-url").value.trim(),
        api_key: el("pf-llm-key").value,
        model: el("pf-llm-model").value.trim(),
        engine: "",
      };
    })(),
```

- [ ] **Step 5: Bind the select's change event**

Near the bottom of `profiles.js`, alongside the other profile-bar event listeners (after line 356, next to the `profile-new-btn` listener), add:

```javascript
if (el("pf-llm-select")) el("pf-llm-select").addEventListener("change", toggleLlmCustomFields);
```

- [ ] **Step 6: Add the `hidden` CSS rule if not already present**

Run: `grep -rn "\.hidden" apps/api_gateway/app/static/*.css apps/api_gateway/app/static/**/*.css 2>/dev/null | head -5`
Expected: a rule like `.hidden { display: none; }` already exists (it's used throughout `profiles.js` for `panel.classList`, `pf-delete-btn`, etc.) — if the grep finds it, no CSS change is needed. If it does NOT find it, add `.hidden { display: none; }` to the main stylesheet file the grep points at for other `.hidden` usages elsewhere in the app (find that file via `grep -rln "class=\"hidden\"\|classList.*hidden" apps/api_gateway/app/static/index.html` to confirm which stylesheet is loaded).

- [ ] **Step 7: Manual browser verification**

Start the dev server per this project's run skill/instructions (check for an existing `run` skill or README dev-server command first). Then:
1. Open the app, go to Profile Configuration, click "+ New".
2. Confirm the LLM select defaults to "— Custom… —" and the 3 free-text fields are visible.
3. In another tab/session (or via the admin Model Registry UI), add an enabled, stable LLM entry (e.g. engine=`openai`, model_id=`gpt-4o-mini`, label=`GPT-4o mini`).
4. Reopen the profile panel (new or edit) — confirm the new entry appears in the LLM select as `GPT-4o mini (openai/gpt-4o-mini)`.
5. Select it, save the profile, reopen it — confirm the select still shows that entry chosen and the custom fields stay hidden.
6. Switch back to "Custom…", confirm the free-text fields reappear and can be filled/saved as before (regression check on today's behavior).
7. As a non-testing user, confirm a `stage="testing"` entry does NOT appear in the dropdown; log in as a user with `can_use_testing=true` (set via admin or `user_store.set_fields`) and confirm it does appear.

- [ ] **Step 8: Commit**

```bash
git add apps/api_gateway/app/static/index.html apps/api_gateway/app/static/js/profiles.js
git commit -m "feat(profiles): pick LLM from model registry in Profile Configuration, with Custom fallback"
```

---

### Task 3: Backend regression check — registry-backed profile save still validates via `check_model_allowed`

**Files:**
- Test: `tests/unit/test_profile_model_gate.py`

This task verifies (does not newly implement) that once the frontend starts sending `llm.engine`, the pre-existing `_validate_profile_models()` → `check_model_allowed()` path in `profiles.py:39-40` behaves correctly end-to-end for the two new scenarios this feature introduces: saving a profile with a registry-backed LLM selection that is disabled or testing-restricted between page load and save.

**Interfaces:**
- Consumes: `check_model_allowed(kind, engine, model_id, user)` (`apps/api_gateway/app/services/model_registry/gate.py:20-31`, already implemented, no changes).

- [ ] **Step 1: Confirm existing coverage**

Run: `cd apps/api_gateway && python -m pytest tests/unit/test_profile_model_gate.py -v`
Expected: all passed. Read the file's existing tests `test_profile_create_rejects_testing_stage_llm_for_non_tester` and `test_profile_create_rejects_disabled_llm_engine` (already present per the design doc) to confirm they already cover: (a) a registry entry in `stage="testing"` rejected for a non-tester with 403, (b) a disabled registry entry rejected. If both already exist and pass, this task requires no new code — it's a documented verification step confirming Task 1/2 don't need additional backend gate work.

- [ ] **Step 2: If either scenario is missing, add it**

Only if the Step 1 read shows a gap (e.g. no test covers a disabled `llm`-kind entry specifically), add this test to `tests/unit/test_profile_model_gate.py` (skip this step entirely if an equivalent test already exists — do not duplicate an already-covered case):

```python
def test_profile_create_rejects_disabled_llm_entry(client, _with_password):
    import asyncio

    from app.services.model_registry.store import ModelRegistryStore

    store = ModelRegistryStore()
    entry = asyncio.run(store.create("llm", "openai", "gpt-4o-mini", "GPT-4o mini", stage="stable"))
    asyncio.run(store.set_fields(entry["id"], enabled=False))
    _signup_login(client, "toan-disabled-llm")
    resp = client.post("/v1/profiles", json={
        "name": "p-disabled-llm",
        "llm": {"engine": "openai", "model": "gpt-4o-mini", "base_url": "https://x", "api_key": ""},
    })
    assert resp.status_code == 403
```

Run: `cd apps/api_gateway && python -m pytest tests/unit/test_profile_model_gate.py -k disabled_llm -v`
Expected: 1 passed

- [ ] **Step 3: No commit needed for this task if Step 1 finds full existing coverage**

If Step 2 added a test, commit it:

```bash
git add tests/unit/test_profile_model_gate.py
git commit -m "test(profiles): verify registry-backed LLM selection respects check_model_allowed"
```
