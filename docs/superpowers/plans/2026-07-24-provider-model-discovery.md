# Provider Model Auto-Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** When adding a Model Registry entry linked to a provider, auto-suggest the provider's available model ids (fetched from its OpenAI-compatible `GET {base_url}/models`) instead of forcing the admin to type the id blind — while always keeping manual entry working.

**Architecture:** A new admin endpoint `GET /v1/providers/{id}/models` fetches the provider's `/models` (using the provider's stored base_url + api_key), parses the OpenAI-compat `data[].id` list, and returns it best-effort (never 500; on any fetch error returns an empty list + an `error` string so the UI falls back to manual). The static Model Registry add-form turns `#registry-add-model-id` into an autocomplete `<input list=...>` backed by a `<datalist>` that is populated on provider selection. Free-text entry always works (datalist = non-restrictive suggestions), which also covers self-hosted providers with no `/models`, and the "hundreds of models" case (OpenRouter) because a datalist filters as you type. Only the static admin console has the registry add-form, so no React change is needed.

**Tech Stack:** FastAPI + httpx (outbound GET), pytest, vanilla ES-module static UI.

## Global Constraints
- `GET /v1/providers/{id}/models` is under the already-admin-gated `/v1/providers` prefix (auth_guard `_ADMIN_PREFIXES`). It uses the provider's REAL api_key internally (via `provider_store.get(id)`, which returns the unmasked entry) but MUST NOT return the key.
- **Best-effort / never 500:** provider not found → 404; any fetch/parse/timeout error → HTTP 200 with `{"success": true, "data": {"models": [], "error": "<message>"}}`. A provider being down must not error the endpoint.
- Manual entry MUST always remain possible (datalist suggestions are non-restrictive; self-hosted / no-`/models` providers just get an empty datalist).
- Static-UI parts: verify `node --check` + grep (NO pytest for the JS). Backend parts: `.venv/bin/python -m pytest` from repo root; `asyncio_mode="auto"`; `_tmp_db` autouse (never a param); sync `TestClient`.
- Git `lugondev <lugondev@gmail.com>`. Concurrent session active — re-check `git branch --show-current` before superproject git-mutating steps. No push (main auto-deploys prod). Don't touch submodules/.dockerignore.

---

### Task 1: Backend `GET /v1/providers/{id}/models`

**Files:**
- Modify: `apps/api_gateway/app/api/routes/providers.py`
- Test: `tests/unit/test_provider_models_route.py`

**Interfaces:**
- `_parse_models(payload: dict) -> list[str]` — pure: from an OpenAI-compat `{"data":[{"id":...},...]}` (or a bare `{"models":[...]}` / list) return the id strings, deduped, order-preserved, dropping blanks.
- `async _fetch_provider_models(base_url: str, api_key: str) -> tuple[list[str], str | None]` — GET `{base_url}/models` with bearer; returns `(ids, None)` on success or `([], "<error>")` on any failure (no raise).
- `GET /v1/providers/{provider_id}/models` → `{"success": True, "data": {"models": [str], "error": str | None}}`; 404 if provider id unknown.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_provider_models_route.py
import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.api.routes import providers as providers_route


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _login_admin(client, username="adm"):
    import asyncio
    from app.services.auth.users import user_store
    client.post("/api/auth/signup", json={"username": username, "password": "pw"})
    user = asyncio.run(user_store.get_by_username(username))
    asyncio.run(user_store.set_fields(user.id, role="admin"))
    client.post("/api/auth/login", json={"username": username, "password": "pw"})


def test_parse_models_openai_shape():
    ids = providers_route._parse_models(
        {"object": "list", "data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}, {"id": ""}, {"nope": 1}]}
    )
    assert ids == ["gpt-4o", "gpt-4o-mini"]


def test_models_route_returns_ids(client, _with_password, monkeypatch):
    _login_admin(client)
    prov = client.post("/v1/providers", json={
        "name": "openai", "base_url": "https://api.openai.com/v1", "api_key": "sk-x",
    }).json()["data"]

    async def fake_fetch(base_url, api_key):
        assert base_url == "https://api.openai.com/v1" and api_key == "sk-x"
        return (["gpt-4o", "gpt-4o-mini"], None)
    monkeypatch.setattr(providers_route, "_fetch_provider_models", fake_fetch)

    resp = client.get(f"/v1/providers/{prov['id']}/models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["models"] == ["gpt-4o", "gpt-4o-mini"]
    assert body["data"]["error"] is None


def test_models_route_fetch_error_is_200_empty(client, _with_password, monkeypatch):
    _login_admin(client)
    prov = client.post("/v1/providers", json={"name": "down", "base_url": "http://x/v1", "api_key": ""}).json()["data"]

    async def boom(base_url, api_key):
        return ([], "connect timeout")
    monkeypatch.setattr(providers_route, "_fetch_provider_models", boom)

    resp = client.get(f"/v1/providers/{prov['id']}/models")
    assert resp.status_code == 200
    assert resp.json()["data"] == {"models": [], "error": "connect timeout"}


def test_models_route_unknown_provider_404(client, _with_password):
    _login_admin(client)
    assert client.get("/v1/providers/does-not-exist/models").status_code == 404
```

- [ ] **Step 2: Run — FAIL** (`.venv/bin/python -m pytest tests/unit/test_provider_models_route.py -v`) — 404 route / missing helpers.

- [ ] **Step 3: Implement** in `routes/providers.py` (add `import httpx` + `import logging` at top if absent; add a module logger):

```python
def _parse_models(payload) -> list[str]:
    """Extract model ids from an OpenAI-compatible /models response.
    Accepts {"data":[{"id":...}]}, {"models":[...]}, or a bare list. Dedupes,
    preserves order, drops blanks/non-str."""
    if isinstance(payload, dict):
        items = payload.get("data")
        if items is None:
            items = payload.get("models", [])
    else:
        items = payload
    out, seen = [], set()
    for it in items or []:
        mid = it.get("id") if isinstance(it, dict) else it
        if isinstance(mid, str) and mid and mid not in seen:
            seen.add(mid)
            out.append(mid)
    return out


async def _fetch_provider_models(base_url: str, api_key: str) -> tuple[list[str], str | None]:
    """GET {base_url}/models (OpenAI-compat). Best-effort: never raises."""
    if not base_url:
        return [], "provider has no base_url"
    url = f"{base_url.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            return [], f"provider returned HTTP {resp.status_code}"
        return _parse_models(resp.json()), None
    except Exception as exc:  # noqa: BLE001 - best-effort discovery
        logger.warning("provider /models fetch failed (%s): %s", url, exc)
        return [], str(exc)


@router.get("/{provider_id}/models")
async def list_provider_models(provider_id: str) -> dict:
    """Best-effort list of the provider's available model ids (from its
    OpenAI-compatible /models). Never 500s: on any fetch error returns an empty
    list + an error message so the UI falls back to manual entry."""
    provider = await provider_store.get(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"provider '{provider_id}' not found")
    models, error = await _fetch_provider_models(provider["base_url"], provider["api_key"])
    return {"success": True, "data": {"models": models, "error": error}}
```
(Ensure `logger = logging.getLogger(__name__)` exists near the top. `provider_store.get` returns the unmasked entry — correct for the internal fetch; the key is never put in the response.)

- [ ] **Step 4: Run — PASS** (4 tests).
- [ ] **Step 5: Regression** — `.venv/bin/python -m pytest tests/unit/test_providers_routes.py -v` (existing provider routes unaffected).
- [ ] **Step 6: Commit** — providers.py + test → `feat(providers): GET /v1/providers/{id}/models best-effort discovery`.

---

### Task 2: Static UI — model_id autocomplete from the selected provider

**Files:**
- Modify: `apps/api_gateway/app/static/index.html` (add `list=` + a `<datalist>`)
- Modify: `apps/api_gateway/app/static/js/model-registry.js`

**Interfaces:** on provider-select change, `GET /v1/providers/{id}/models` → fill `<datalist id="registry-model-suggestions">`; empty/error → clear datalist (input stays free-text) + a hint in the status line.

- [ ] **Step 1: index.html** — give the model-id input a datalist and add the datalist element. Change line ~911:

```html
                  <input id="registry-add-model-id" type="text" placeholder="qwen3-asr-flash" list="registry-model-suggestions" autocomplete="off" />
                  <datalist id="registry-model-suggestions"></datalist>
```
(Read the surrounding label block first; keep the `<label>Model ID …</label>` wrapper intact — just add the attribute + datalist inside/after the input.)

- [ ] **Step 2: model-registry.js** — add a loader and call it from the provider-change handler. Near `_loadProviderOptions`, add:

```javascript
async function _loadProviderModelSuggestions() {
  const dl = el("registry-model-suggestions");
  const providerId = (el("registry-add-provider")?.value || "").trim();
  if (!dl) return;
  dl.innerHTML = "";
  if (!providerId) return; // no provider -> plain free-text input
  const status = el("model-registry-status");
  try {
    const resp = await fetch(`/v1/providers/${encodeURIComponent(providerId)}/models`);
    const body = await resp.json();
    const models = (body.data && body.data.models) || [];
    dl.innerHTML = models.map((m) => `<option value="${escapeHtml(String(m))}"></option>`).join("");
    if (body.data && body.data.error) {
      print(status, `Couldn't load models (${body.data.error}) — type the model id manually.`, true);
    } else if (models.length && status) {
      status.textContent = `Loaded ${models.length} model(s) from provider — pick or type.`;
    }
  } catch (e) {
    // network error -> leave datalist empty; manual entry still works
    print(el("model-registry-status"), `Couldn't load models (${e}) — type the model id manually.`, true);
  }
}
```
Then in the existing `registry-add-provider` change listener (currently `... addEventListener("change", _updateKindFields)`), ALSO trigger the suggestion load. Change it to a handler that calls both:

```javascript
if (el("registry-add-provider")) {
  el("registry-add-provider").addEventListener("change", () => {
    _updateKindFields();
    void _loadProviderModelSuggestions();
  });
}
```
(Confirm `escapeHtml` and `print` are already imported in model-registry.js — they are.)

- [ ] **Step 3: Verify** — `node --check apps/api_gateway/app/static/js/model-registry.js` (OK); grep `registry-model-suggestions` present in BOTH index.html and model-registry.js; confirm the input still has no `required`/restrictive attribute (free text preserved).

- [ ] **Step 4: Commit** — index.html + model-registry.js → `feat(admin-ui): autocomplete model id from the selected provider's /models`.

---

### Task 3: Verify (controller)
- [ ] `.venv/bin/python -m pytest tests/unit/test_provider_models_route.py tests/unit/test_providers_routes.py -q` (pass) + `.venv/bin/python -c "import app.main"` + `node --check apps/api_gateway/app/static/js/model-registry.js`.

## Deferred
- Caching the models list per provider (re-fetches on each provider-select; fine — admin action, infrequent).
- Filtering by capability/kind (OpenRouter returns all model types; the datalist + type-ahead already makes this usable; a kind filter could come later).

## Self-Review
- **Coverage:** the missing piece from the earlier question — auto-load models when adding a registry entry — is delivered: backend discovery endpoint (T1) + autocomplete UI (T2), with manual entry always preserved (datalist non-restrictive) and best-effort error handling (never 500 / never blocks the form).
- **Placeholder scan:** T1 complete code; T2 complete code + a "read the label block first" note for the one HTML edit.
- **Consistency:** endpoint shape `{success, data:{models, error}}` consumed exactly in T2 (`body.data.models` / `body.data.error`); datalist id `registry-model-suggestions` matches between index.html and model-registry.js; provider api_key used internally but never returned.
