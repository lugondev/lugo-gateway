# Show the resolved server defaults (not the opaque "server default")

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** In the conversation engine-summary line, replace the opaque `"server default"` for STT/TTS/LLM with the ACTUAL resolved default (e.g. `STT: Whisper large-v3-turbo (default)`), so the admin sees what the server will actually use.

**Why:** `conversation.js::updateConvEnginesInfo` prints the literal string `"server default"` whenever a profile/selection doesn't pin STT/TTS/LLM — it never resolves what the default is. The backend DOES resolve it at session time (`system_config default_{stt,tts}_engine` + `resolve_default_stt_model`, `find_default("llm")`), but that isn't exposed to the UI.

**Architecture:** Add a read-only `GET /v1/model_registry/defaults` (user-accessible, carved into `_USER_PREFIXES` like `/options`) returning the resolved `{stt, tts, llm}` defaults with friendly labels. `conversation.js` fetches it alongside `convCatalog` and shows the labels (with a `(default)` marker) in place of `"server default"`.

## Global Constraints
- Backend: tests from repo ROOT `.venv/bin/python -m pytest`; asyncio auto; `_tmp_db` autouse (never a param); sync `TestClient`. Frontend: `node --check` + grep, NO pytest.
- New endpoint is READ-ONLY + user-accessible (any logged-in user — the conversation UI is user-facing): add `/v1/model_registry/defaults` to `_USER_PREFIXES` in `core/auth_guard.py` (checked before the admin `/v1/model_registry` rule, same as `/v1/model_registry/options`).
- Defaults resolution (mirror what a session does): stt engine = `system_config.engines.default_stt_engine`, stt model = `resolve_default_stt_model(engine)` (may be None → ""); tts engine = `default_tts_engine`; llm = `model_registry_store.find_default("llm")` (may be None). Label = the registry entry's label if found, else `engine/model` (or `engine` if no model).
- Display: show `"<label> (default)"` where the value is a server default; keep the existing explicit-selection labels unchanged; if defaults haven't loaded, fall back to `"server default"` (no regression).
- Git `lugondev <lugondev@gmail.com>`. Concurrent session — re-check branch before git. No push (main auto-deploys prod).

---

### Task 1: Backend `GET /v1/model_registry/defaults`

**Files:** Modify `apps/api_gateway/app/api/routes/model_registry.py` + `apps/api_gateway/app/core/auth_guard.py`; Test `tests/unit/test_model_registry_defaults.py`.

**Interfaces:** `GET /v1/model_registry/defaults` → `{"success": True, "data": {"stt": {engine, model_id, label}, "tts": {engine, label}, "llm": {engine, model_id, label} | null}}`.

- [ ] **Step 1: Failing test**
```python
# tests/unit/test_model_registry_defaults.py
from fastapi.testclient import TestClient
from app.core.settings import settings
from app.main import app
from app.services.model_registry.store import model_registry_store


def _client():
    return TestClient(app)


def _login(client, username="u", role="user"):
    import asyncio
    from app.services.auth.users import user_store
    client.post("/api/auth/signup", json={"username": username, "password": "pw"})
    if role == "admin":
        u = asyncio.run(user_store.get_by_username(username))
        asyncio.run(user_store.set_fields(u.id, role="admin"))
    client.post("/api/auth/login", json={"username": username, "password": "pw"})


def test_defaults_shape_and_user_accessible(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    client = _client()
    _login(client, role="user")  # NON-admin: /defaults must be reachable
    resp = client.get("/v1/model_registry/defaults")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert set(data.keys()) == {"stt", "tts", "llm"}
    assert "engine" in data["stt"] and "label" in data["stt"]
    assert "engine" in data["tts"] and "label" in data["tts"]
    # llm is null unless an is_default llm entry exists


async def test_defaults_reflect_default_llm_entry():
    from app.services.db.engine import init_db
    await init_db()
    await model_registry_store.create("llm", "openrouter", "x/y", "My LLM", is_default=True)
    from app.api.routes.model_registry import get_defaults  # direct call, avoids auth
    # get_defaults takes a Request; build a dummy with a minimal shim if needed,
    # OR assert via the TestClient path above. Prefer: call summarize logic through
    # the store; simplest is to assert model_registry_store.find_default returns it:
    d = await model_registry_store.find_default("llm")
    assert d and d["engine"] == "openrouter" and d["model_id"] == "x/y"
```
(If `get_defaults` needs a `Request` and that's awkward to construct in a unit test, keep the route-level test via `TestClient` as the primary assertion and drop the direct-call test — state the choice in the report. The must-haves: 200 for a non-admin, correct shape, llm reflects `find_default`.)

- [ ] **Step 2: Run — FAIL** (route missing / 403).

- [ ] **Step 3: Implement** the route in `routes/model_registry.py` (near the other `@router.get` handlers):
```python
@router.get("/defaults")
async def get_defaults() -> dict:
    """The server's resolved default STT/TTS/LLM — what a session uses when a
    profile/selection doesn't pin one. Read-only; lets the UI show the actual
    model behind "server default"."""
    from app.services.stt.model_catalog import resolve_default_stt_model
    from app.services.system_config import system_config_store

    async def _label(kind: str, engine: str, model_id: str) -> str:
        if not engine:
            return ""
        entry = await model_registry_store.find(kind, engine, model_id or "")
        if entry and entry.get("label"):
            return entry["label"]
        return f"{engine}/{model_id}" if model_id else engine

    eng = system_config_store.get().engines
    stt_engine = eng.default_stt_engine
    stt_model = resolve_default_stt_model(stt_engine) or ""
    tts_engine = eng.default_tts_engine
    llm = await model_registry_store.find_default("llm")
    return {
        "success": True,
        "data": {
            "stt": {"engine": stt_engine, "model_id": stt_model, "label": await _label("stt", stt_engine, stt_model)},
            "tts": {"engine": tts_engine, "label": await _label("tts", tts_engine, "")},
            "llm": (
                {"engine": llm["engine"], "model_id": llm["model_id"],
                 "label": llm.get("label") or await _label("llm", llm["engine"], llm["model_id"])}
                if llm else None
            ),
        },
    }
```

- [ ] **Step 4: auth_guard** — add `"/v1/model_registry/defaults"` to the `_USER_PREFIXES` tuple in `core/auth_guard.py` (next to `"/v1/model_registry/options"`).

- [ ] **Step 5: Run — PASS**; regression `.venv/bin/python -m pytest tests/unit/test_model_registry_routes.py -q`.

- [ ] **Step 6: Commit** — model_registry.py + auth_guard.py + test → `feat(model-registry): GET /defaults (resolved server default stt/tts/llm)`.

---

### Task 2: Frontend — display the resolved defaults

**Files:** Modify `apps/api_gateway/app/static/js/conversation.js`.

- [ ] **Step 1: Read** `convCatalog`/`convDetails`, the loader that fetches `/v1/model_registry/options` (~line 143-153), and `updateConvEnginesInfo` (~169-192).

- [ ] **Step 2: Fetch + store defaults.** Add near `convCatalog`:
```javascript
export const convServerDefaults = { stt: null, tts: null, llm: null };
```
In the loader (the same function that fills `convCatalog` from `/options`), also fetch defaults:
```javascript
  try {
    const body = await (await fetch("/v1/model_registry/defaults")).json();
    if (body.success && body.data) {
      convServerDefaults.stt = body.data.stt || null;
      convServerDefaults.tts = body.data.tts || null;
      convServerDefaults.llm = body.data.llm || null;
    }
  } catch { /* leave nulls -> falls back to "server default" text */ }
```

- [ ] **Step 3: A default-label helper** (near `catalogLabel`):
```javascript
// The server default for a kind, shown with a "(default)" marker so the user
// can tell it apart from an explicit per-profile/manual selection.
function defaultLabel(kind) {
  const d = convServerDefaults[kind];
  const lbl = d && d.label;
  return lbl ? `${lbl} (default)` : "server default";
}
```

- [ ] **Step 4: Use it in `updateConvEnginesInfo`.** Replace the `"server default"` fallbacks:
  - Profile branch:
    ```javascript
    const sttLabel = catalogLabel("stt", p.stt?.engine || "", p.stt?.model || "") || defaultLabel("stt");
    const llmLabel = catalogLabel("llm", p.llm?.engine || "", p.llm?.model || "") || p.llm?.model || defaultLabel("llm");
    const ttsLabel = p.tts?.profile_name || defaultLabel("tts");
    ```
  - No-profile branch:
    ```javascript
    const sttPart = `STT: ${defaultLabel("stt")}`;
    const llmPart = convDetails.llm ? `LLM: ${convDetails.llm}` : `LLM: ${defaultLabel("llm")}`;
    const ttsPart = `TTS: ${ttsProfileName || defaultLabel("tts")}`;
    ```

- [ ] **Step 5: Verify** — `node --check apps/api_gateway/app/static/js/conversation.js` (OK); grep `convServerDefaults` fetched from `/v1/model_registry/defaults` + `defaultLabel` used in both branches; confirm the fallback to `"server default"` remains when defaults haven't loaded.

- [ ] **Step 6: Commit** — conversation.js → `feat(admin-ui): show resolved server default (model) instead of opaque "server default"`.

---

### Task 3: Verify (controller)
- [ ] `.venv/bin/python -m pytest tests/unit/test_model_registry_defaults.py tests/unit/test_model_registry_routes.py -q`; `node --check apps/api_gateway/app/static/js/conversation.js`; `.venv/bin/python -c "import app.main"`.

## Self-Review
- **Coverage:** the opaque `"server default"` now resolves to the actual default model+label (with a `(default)` marker); backend endpoint mirrors session-time resolution; user-accessible (non-admin conversation UI). Falls back to `"server default"` only if the endpoint fails to load (no regression).
- **Placeholders:** complete code both tasks (Task 1 test has a stated fallback for the Request-construction awkwardness).
- **Consistency:** endpoint shape `{stt:{engine,model_id,label}, tts:{engine,label}, llm:{...}|null}` consumed by `convServerDefaults` + `defaultLabel`; `/v1/model_registry/defaults` carved into `_USER_PREFIXES`; both summary branches use `defaultLabel`.
