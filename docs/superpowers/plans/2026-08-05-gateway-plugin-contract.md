# Gateway Plugin Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the gateway an out-of-process plugin contract, then move Livehost out of the gateway into `servers/livehost-api` as the first plugin.

**Architecture:** A plugin is a separate service registered in a `SqliteBackedStore[Plugin]` row. The browser asks the gateway for a short-lived, plugin-scoped ticket and connects straight to the plugin. The plugin exchanges that ticket for a `user_id` via `POST /api/auth/introspect`, then opens one upstream `WS /v1/conversation/stream` per session and relays audio and events between browser and gateway. All voice machinery — STT, endpointing, LLM, TTS with pacing, quota, usage, history, memory injection — stays in the gateway and is reached only through that socket.

**Tech Stack:** Python ≥3.10, FastAPI, Starlette WebSockets, pydantic v2, SQLAlchemy (sync, via `session_scope`), itsdangerous (token signing), httpx + websockets (plugin-side upstream client), pytest with `asyncio_mode = "auto"`, ruff at line-length 100.

**Spec:** `docs/superpowers/specs/2026-08-05-gateway-plugin-contract-design.md`

## Global Constraints

- Every HTTP response uses the envelope `{"success": True, "data": ...}`.
- Gateway tests run from the repo root with the repo's own interpreter — `/Users/lugon/code/speech-text-transformer/.venv/bin/python -m pytest`. A bare `python` resolves to a pyenv shim that lacks the dependencies, and a git worktree has no `.venv` of its own.
- pytest config: `pythonpath = ["apps/api_gateway", "apps"]`, `asyncio_mode = "auto"`. The `timeout = 120` is **per test**, not for the whole run; the full suite is ~1978 tests and takes about 3.5 minutes.
- `tests/conftest.py` provides three autouse fixtures: `_hermetic`, `_hermetic_engine_health`, `_tmp_db`. Any store singleton touched in a test must be `.invalidate()`d, because the singletons outlive the per-test database.
- Plugins MUST NOT import gateway internals. The only gateway surface a plugin may use is `WS /v1/conversation/stream`, `POST /api/auth/introspect`, `GET /v1/profiles`, `GET /v1/tts_profiles`.
- `tests/unit/http/test_auth_guard_route_coverage.py` walks the real route table and fails if any mounted HTTP path is unclassified by `app/core/auth_guard.py`. New routes must be classified in the same commit.
- Ruff line-length is 100. Run `ruff check` and `ruff format --check` before every commit.
- The gateway keeps working with Livehost in-process until Task 12. Tasks 1–11 must leave the full suite green.

---

# Phase 1 — The gateway grows the contract

## Task 1: Plugin model and store

**Files:**
- Create: `apps/api_gateway/app/services/plugins/__init__.py` (empty)
- Create: `apps/api_gateway/app/services/plugins/models.py`
- Create: `apps/api_gateway/app/services/plugins/store.py`
- Modify: `apps/api_gateway/app/services/db/config_models.py` (append `PluginRow`)
- Test: `tests/unit/plugins/__init__.py` (empty), `tests/unit/plugins/test_plugin_store.py`

**Interfaces:**
- Consumes: `SqliteBackedStore` from `app.services.db.config_store` — `__init__(path=None, *, row_cls, model_cls, key_attr, legacy_parse, settings_attr=None)`, methods `list() -> dict[str, M]`, `get(name) -> M | None`, `exists(name) -> bool`, `upsert(model) -> None`, `delete(name) -> None`, `invalidate() -> None`.
- Produces: `Plugin`, `PluginMount`, `PluginStore`, and the module-level singleton `plugin_store`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/plugins/__init__.py` as an empty file, then `tests/unit/plugins/test_plugin_store.py`:

```python
import pytest

from app.services.plugins.models import Plugin, PluginMount
from app.services.plugins.store import plugin_store


@pytest.fixture(autouse=True)
def _fresh_store():
    plugin_store.invalidate()
    yield
    plugin_store.invalidate()


def _plugin(name: str = "livehost", **over) -> Plugin:
    data = {
        "name": name,
        "url": "http://127.0.0.1:8091",
        "secret": "s3cret",
        "mounts": [PluginMount(path="/v1/livehost/stream", kind="ws")],
    }
    data.update(over)
    return Plugin(**data)


def test_roundtrip_through_the_store():
    plugin_store.upsert(_plugin())
    got = plugin_store.get("livehost")
    assert got is not None
    assert got.url == "http://127.0.0.1:8091"
    assert got.secret == "s3cret"
    assert got.enabled is True
    assert got.kind == "feature"
    assert got.mounts[0].path == "/v1/livehost/stream"
    assert got.mounts[0].kind == "ws"


def test_exists_reports_occupancy_for_the_409_check():
    assert plugin_store.exists("livehost") is False
    plugin_store.upsert(_plugin())
    assert plugin_store.exists("livehost") is True


def test_delete_removes_the_row():
    plugin_store.upsert(_plugin())
    plugin_store.delete("livehost")
    assert plugin_store.get("livehost") is None
    assert plugin_store.exists("livehost") is False


def test_list_returns_every_plugin_keyed_by_name():
    plugin_store.upsert(_plugin("livehost"))
    plugin_store.upsert(_plugin("lugo", url="http://127.0.0.1:8092"))
    assert sorted(plugin_store.list()) == ["livehost", "lugo"]


@pytest.mark.parametrize("bad", ["ftp://x", "file:///etc/passwd", "not-a-url"])
def test_url_scheme_is_refused(bad):
    with pytest.raises(ValueError):
        _plugin(url=bad)


def test_https_url_is_accepted():
    assert _plugin(url="https://livehost.internal:8091").url.startswith("https://")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/plugins/test_plugin_store.py -v`
Expected: FAIL, collection error `ModuleNotFoundError: No module named 'app.services.plugins'`

- [ ] **Step 3: Add the SQLAlchemy row**

Append to `apps/api_gateway/app/services/db/config_models.py`, after `McpServerRow`:

```python
class PluginRow(ConfigBase):
    __tablename__ = "config_plugins"
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    data: Mapped[str] = mapped_column(Text)
```

- [ ] **Step 4: Write the model**

Create `apps/api_gateway/app/services/plugins/models.py`:

```python
"""A registered out-of-process plugin.

Deliberately shaped after McpServer (services/mcp/models.py): same name/owner/
url/enabled spine, same url-scheme validation. The one difference is direction.
McpServer.headers is a credential the gateway SENDS when it calls the MCP
server. `secret` here runs the other way -- the gateway never calls a plugin,
the browser does, so the only cross-service call is the plugin calling back
into POST /api/auth/introspect, and `secret` is what authenticates it.
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, field_validator


class PluginMount(BaseModel):
    """One path the plugin serves. Advertised to the web client by
    GET /v1/plugins so it knows what to connect to without hardcoding it."""

    path: str
    kind: Literal["ws", "http"]
    public: bool = True


class Plugin(BaseModel):
    name: str
    owner_id: str | None = None
    url: str
    secret: str
    enabled: bool = True
    # "tools" exists so the MCP server registry can fold into this store later.
    # Nothing in this design depends on that happening.
    kind: Literal["feature", "tools"] = "feature"
    mounts: list[PluginMount] = []

    @field_validator("url")
    @classmethod
    def validate_url_scheme(cls, v: str) -> str:
        if urlparse(v).scheme not in ("http", "https"):
            raise ValueError("Plugin URL must use http or https scheme")
        return v
```

- [ ] **Step 5: Write the store**

Create `apps/api_gateway/app/services/plugins/store.py`:

```python
"""The fourth SqliteBackedStore. No legacy-JSON predecessor exists for plugins,
so `legacy_parse` is never reachable: with no `path` and no `settings_attr`,
_resolve_path() returns None and _ensure() skips the import branch entirely."""

from __future__ import annotations

from app.services.db.config_models import PluginRow
from app.services.db.config_store import SqliteBackedStore
from app.services.plugins.models import Plugin


def _no_legacy(path: str) -> dict[str, dict]:
    return {}


class PluginStore(SqliteBackedStore[Plugin]):
    def __init__(self, path: str | None = None) -> None:
        super().__init__(
            path, row_cls=PluginRow, model_cls=Plugin,
            key_attr="name", legacy_parse=_no_legacy,
        )


plugin_store = PluginStore()
```

Create `apps/api_gateway/app/services/plugins/__init__.py` as an empty file.

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/unit/plugins/test_plugin_store.py -v`
Expected: PASS, 8 passed

- [ ] **Step 7: Run the full suite and lint**

Run: `pytest -q && ruff check apps/api_gateway/app tests && ruff format --check apps/api_gateway/app tests`
Expected: all pass. `init_config_tables()` creates `config_plugins` automatically from the new `PluginRow`.

- [ ] **Step 8: Commit**

```bash
git add apps/api_gateway/app/services/plugins apps/api_gateway/app/services/db/config_models.py tests/unit/plugins
git commit -m "feat(plugins): a registry row for an out-of-process plugin"
```

---

## Task 2: Plugin tickets

**Files:**
- Modify: `apps/api_gateway/app/services/auth/tokens.py`
- Test: `tests/unit/auth/test_plugin_tokens.py`

**Interfaces:**
- Consumes: the private `_issue(user_id, salt)` and `_verify(token, salt, max_age)` helpers already in `tokens.py`.
- Produces: `PLUGIN_TICKET_TTL_SECONDS: int`, `issue_plugin_token(user_id: str, plugin: str) -> str`, `verify_plugin_token(token: str, plugin: str) -> str | None`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/auth/test_plugin_tokens.py`:

```python
from app.services.auth.tokens import (
    PLUGIN_TICKET_TTL_SECONDS,
    issue_access_token,
    issue_plugin_token,
    verify_access_token,
    verify_plugin_token,
)


def test_a_ticket_round_trips_for_its_own_plugin():
    token = issue_plugin_token("user-1", "livehost")
    assert verify_plugin_token(token, "livehost") == "user-1"


def test_a_ticket_minted_for_one_plugin_is_worthless_at_another():
    token = issue_plugin_token("user-1", "livehost")
    assert verify_plugin_token(token, "lugo") is None


def test_a_ticket_is_not_an_access_token():
    """Salt separation runs both ways: a ticket must not open the bearer path,
    and an access token must not open a plugin."""
    ticket = issue_plugin_token("user-1", "livehost")
    assert verify_access_token(ticket) is None
    access = issue_access_token("user-1")
    assert verify_plugin_token(access, "livehost") is None


def test_an_expired_ticket_is_refused(monkeypatch):
    """Proves the TTL is actually consulted, not merely that _verify catches
    SignatureExpired. verify_plugin_token reads the module-level constant at
    call time, so shrinking it below zero makes a ticket issued a moment ago
    genuinely expired -- through real itsdangerous, with no clock to wait on
    and no shared class left patched.

    The discriminating property matters more than the green tick: patching a
    serializer to raise unconditionally would pass whether the code threaded
    through PLUGIN_TICKET_TTL_SECONDS or ACCESS_TTL_SECONDS, so it would guard
    nothing. Prove this one bites by swapping the constant and watching it
    fail before you trust it.
    """
    from app.services.auth import tokens

    token = issue_plugin_token("user-1", "livehost")
    assert verify_plugin_token(token, "livehost") == "user-1"

    monkeypatch.setattr(tokens, "PLUGIN_TICKET_TTL_SECONDS", -1)
    assert verify_plugin_token(token, "livehost") is None


def test_garbage_is_refused():
    assert verify_plugin_token("", "livehost") is None
    assert verify_plugin_token("not-a-token", "livehost") is None


def test_the_ttl_is_short_because_the_ticket_travels_in_a_query_string():
    assert PLUGIN_TICKET_TTL_SECONDS <= 300
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/auth/test_plugin_tokens.py -v`
Expected: FAIL with `ImportError: cannot import name 'PLUGIN_TICKET_TTL_SECONDS'`

- [ ] **Step 3: Implement**

Append to `apps/api_gateway/app/services/auth/tokens.py`:

```python
# Vé một-lần cho plugin (xem specs/2026-08-05-gateway-plugin-contract-design.md).
# TTL ngắn có chủ đích: browser không set được header trên WebSocket handshake,
# nên vé đi trong query string và nằm lại trong access log. Nó mua đúng một lần
# kết nối, không phải một phiên -- phiên sống theo socket, không theo vé.
PLUGIN_TICKET_TTL_SECONDS = 60


def _plugin_salt(plugin: str) -> str:
    """Audience binding là salt, không phải claim mới: cùng cơ chế đã tách
    access khỏi refresh ở trên. Vé đúc cho 'livehost' hỏng chữ ký dưới salt của
    bất kỳ plugin nào khác, và người verify phải gọi tên plugin nó chờ đợi --
    nên phép kiểm tra đó không thể quên được."""
    return f"lugo-plugin:{plugin}"


def issue_plugin_token(user_id: str, plugin: str) -> str:
    return _issue(user_id, _plugin_salt(plugin))


def verify_plugin_token(token: str, plugin: str) -> str | None:
    return _verify(token, _plugin_salt(plugin), PLUGIN_TICKET_TTL_SECONDS)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/auth/test_plugin_tokens.py -v`
Expected: PASS, 6 passed

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/auth/tokens.py tests/unit/auth/test_plugin_tokens.py
git commit -m "feat(auth): plugin tickets, audience-bound by salt"
```

---

## Task 3: Ticket introspection

**Files:**
- Modify: `apps/api_gateway/app/schemas/auth.py` (append `IntrospectRequest`)
- Modify: `apps/api_gateway/app/api/routes/auth.py` (append the route)
- Modify: `tests/conftest.py` (add `plugin_store` to `_tmp_db`'s invalidate list)
- Test: `tests/unit/auth/test_auth_introspect.py`

**Interfaces:**
- Consumes: `plugin_store`, `Plugin` (Task 1); `verify_plugin_token` (Task 2).
- Produces: `POST /api/auth/introspect` taking `{"token": str, "plugin": str}` with an `Authorization: Bearer <Plugin.secret>` header, returning `{"success": True, "data": {"active": bool, "user_id": str | None}}`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/auth/test_auth_introspect.py`:

```python
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.auth.tokens import issue_plugin_token
from app.services.plugins.models import Plugin
from app.services.plugins.store import plugin_store


@pytest.fixture(autouse=True)
def _registered_plugin():
    plugin_store.invalidate()
    plugin_store.upsert(
        Plugin(name="livehost", url="http://127.0.0.1:8091", secret="plugin-secret")
    )
    yield
    plugin_store.invalidate()


def _post(client, token, plugin="livehost", secret="plugin-secret"):
    return client.post(
        "/api/auth/introspect",
        json={"token": token, "plugin": plugin},
        headers={"Authorization": f"Bearer {secret}"},
    )


def test_a_valid_ticket_resolves_to_its_user():
    with TestClient(app) as client:
        r = _post(client, issue_plugin_token("user-1", "livehost"))
        assert r.status_code == 200
        assert r.json()["data"] == {"active": True, "user_id": "user-1"}


def test_a_ticket_for_another_plugin_is_inactive():
    with TestClient(app) as client:
        r = _post(client, issue_plugin_token("user-1", "lugo"))
        assert r.status_code == 200
        assert r.json()["data"]["active"] is False
        assert r.json()["data"]["user_id"] is None


def test_a_wrong_plugin_secret_is_401():
    """The whole reason introspection is authenticated: /api/auth sits in
    _NO_AUTH_PREFIXES, so without this an anyone-who-read-a-log lookup turns a
    ticket into a user_id."""
    with TestClient(app) as client:
        r = _post(client, issue_plugin_token("user-1", "livehost"), secret="wrong")
        assert r.status_code == 401


def test_a_missing_authorization_header_is_401():
    with TestClient(app) as client:
        r = client.post(
            "/api/auth/introspect",
            json={"token": issue_plugin_token("user-1", "livehost"), "plugin": "livehost"},
        )
        assert r.status_code == 401


def test_an_unknown_plugin_is_401_not_404():
    """Same response as a bad secret: an unauthenticated caller must not be
    able to enumerate which plugins are registered."""
    with TestClient(app) as client:
        r = _post(client, "whatever", plugin="nope")
        assert r.status_code == 401


def test_a_disabled_plugin_cannot_introspect():
    plugin_store.upsert(
        Plugin(name="livehost", url="http://127.0.0.1:8091",
               secret="plugin-secret", enabled=False)
    )
    with TestClient(app) as client:
        r = _post(client, issue_plugin_token("user-1", "livehost"))
        assert r.status_code == 401


def test_garbage_token_is_inactive_not_an_error():
    with TestClient(app) as client:
        r = _post(client, "not-a-token")
        assert r.status_code == 200
        assert r.json()["data"]["active"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/auth/test_auth_introspect.py -v`
Expected: FAIL, every test 404s — the route does not exist.

- [ ] **Step 3: Add the request schema**

Append to `apps/api_gateway/app/schemas/auth.py`:

```python
class IntrospectRequest(BaseModel):
    token: str
    plugin: str
```

- [ ] **Step 4: Implement the route**

Add to the imports at the top of `apps/api_gateway/app/api/routes/auth.py`:

```python
import hmac

from app.schemas.auth import IntrospectRequest
from app.services.auth.tokens import verify_plugin_token
from app.services.plugins.store import plugin_store
```

(Merge `IntrospectRequest` into the existing `app.schemas.auth` import line rather than adding a second one, and keep `hmac` in the stdlib block.)

Append the route:

```python
@router.post("/introspect")
async def introspect(body: IntrospectRequest, request: Request) -> dict:
    """Đổi vé plugin lấy user_id. Người gọi là plugin, không phải người dùng.

    Endpoint này nằm dưới /api/auth, tức là trong _NO_AUTH_PREFIXES (đăng nhập
    buộc phải ở đó). Nên nó tự xác thực bằng Plugin.secret: thiếu bước này, bất
    kỳ ai đọc được vé trong access log đều tra ra được user_id.

    Plugin lạ, plugin đã tắt, và secret sai đều trả về đúng một phản hồi 401 --
    một người gọi chưa xác thực không được phép dò xem plugin nào đang đăng ký.
    """
    entry = plugin_store.get(body.plugin)
    header = request.headers.get("Authorization", "")
    scheme, _, presented = header.partition(" ")
    if (
        entry is None
        or not entry.enabled
        or scheme.lower() != "bearer"
        or not hmac.compare_digest(presented.strip(), entry.secret)
    ):
        raise AuthError("invalid plugin credentials")
    user_id = verify_plugin_token(body.token, body.plugin)
    return {"success": True, "data": {"active": user_id is not None, "user_id": user_id}}
```

Confirm `AuthError` is already imported in this module (it is used by `refresh`); if the file imports it from `app.core.errors`, reuse that import. Confirm `Request` is imported — `token` and `login` already take one.

- [ ] **Step 5: Register `plugin_store` with the per-test DB fixture**

This task is the first where a *route* reaches `plugin_store`, so the shared fixture has to know about it. In `tests/conftest.py`'s `_tmp_db` fixture, import `plugin_store` alongside the other config stores and invalidate it with them:

```python
    from app.services.plugins.store import plugin_store
```

```python
    profile_store.invalidate()
    tts_profile_store.invalidate()
    mcp_server_store.invalidate()
    plugin_store.invalidate()
```

Without it, the store's process-global cache — warmed by whichever test touched it first, against *that* test's tmp DB — leaks into every later test, and those tests never re-run `init_config_tables()` against their own DB. The fixture's existing comment names the exact failure: `"no such table: config_profiles"`. `plugin_store` needs no `settings_attr` path repointing, because it has no legacy-JSON predecessor.

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/unit/auth/test_auth_introspect.py -v`
Expected: PASS, 7 passed

- [ ] **Step 7: Verify AuthError maps to 401**

Run: `pytest tests/unit/auth/test_auth_introspect.py::test_a_wrong_plugin_secret_is_401 -v`
Expected: PASS. If it reports 400 instead, `AuthError` is not mapped to 401 by the app's exception handler — in that case raise `HTTPException(status_code=401, detail="invalid plugin credentials")` instead and keep the tests as written.

- [ ] **Step 8: Commit**

```bash
git add apps/api_gateway/app/api/routes/auth.py apps/api_gateway/app/schemas/auth.py tests/conftest.py tests/unit/auth/test_auth_introspect.py
git commit -m "feat(auth): introspect a plugin ticket, authenticated by plugin secret"
```

---

## Task 4: Plugin routes, wiring and auth classification

**Files:**
- Create: `apps/api_gateway/app/schemas/plugins.py`
- Create: `apps/api_gateway/app/api/routes/plugins.py`
- Modify: `apps/api_gateway/app/main.py` (import + `include_router`)
- Modify: `apps/api_gateway/app/core/auth_guard.py` (`_ADMIN_PREFIXES`, `_USER_EXACT`)
- Test: `tests/unit/plugins/test_plugins_routes.py`

**Interfaces:**
- Consumes: `plugin_store`, `Plugin`, `PluginMount` (Task 1); `issue_plugin_token`, `PLUGIN_TICKET_TTL_SECONDS` (Task 2); `current_user_id`, `current_role`, `require_admin` from `app.core.actor`.
- Produces: `GET|POST /v1/plugins`, `GET|PUT|DELETE /v1/plugins/{name}`, `POST /v1/plugins/ticket`.

**Why the ticket path has no `{name}`:** `/v1/plugins` becomes an admin prefix, and `auth_guard._USER_EXACT` — the only mechanism for a user carve-out inside an admin prefix — matches exactly and by method, never as a prefix. That is deliberate: its comments document bug class M1, where a path parameter lets a non-admin reach an admin handler. A carve-out cannot be written for a parameterized path, so the plugin name travels in the request body.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/plugins/test_plugins_routes.py`:

```python
import pytest
from fastapi.testclient import TestClient

from app.core.auth_guard import _ADMIN_PREFIXES, _USER_EXACT, _classify
from app.main import app
from app.services.auth.tokens import verify_plugin_token
from app.services.plugins.models import Plugin
from app.services.plugins.store import plugin_store


@pytest.fixture(autouse=True)
def _fresh_store():
    plugin_store.invalidate()
    yield
    plugin_store.invalidate()


def _seed(name="livehost", **over):
    data = {"name": name, "url": "http://127.0.0.1:8091", "secret": "plugin-secret"}
    data.update(over)
    plugin_store.upsert(Plugin(**data))


# --- auth classification (pure, no client needed) ---

def test_the_plugins_prefix_is_admin():
    assert "/v1/plugins" in _ADMIN_PREFIXES
    assert _classify("/v1/plugins/x", "PUT") == "admin"
    assert _classify("/v1/plugins/x", "DELETE") == "admin"


def test_listing_is_a_user_carve_out_for_reads_only():
    assert _USER_EXACT["/v1/plugins"] == frozenset({"GET", "HEAD"})
    assert _classify("/v1/plugins", "GET") == "user"
    assert _classify("/v1/plugins", "POST") == "admin"


def test_the_ticket_carve_out_is_post_only():
    """GET/PUT/DELETE /v1/plugins/ticket would route to the /{name} handlers
    with name='ticket'. Restricting the carve-out to POST -- for which no
    /{name} route exists -- is what stops that shadowing."""
    assert _USER_EXACT["/v1/plugins/ticket"] == frozenset({"POST"})
    assert _classify("/v1/plugins/ticket", "POST") == "user"
    assert _classify("/v1/plugins/ticket", "DELETE") == "admin"


# --- routes ---

def test_list_masks_the_secret_from_non_admins(user_client):
    _seed()
    r = user_client.get("/v1/plugins")
    assert r.status_code == 200
    assert r.json()["data"]["livehost"]["secret"] == "***"


def test_list_shows_the_secret_to_admins(admin_client):
    _seed()
    r = admin_client.get("/v1/plugins")
    assert r.json()["data"]["livehost"]["secret"] == "plugin-secret"


def test_create_requires_admin(user_client):
    r = user_client.post(
        "/v1/plugins",
        json={"name": "livehost", "url": "http://127.0.0.1:8091", "secret": "s"},
    )
    assert r.status_code == 403


def test_create_then_duplicate_is_409(admin_client):
    body = {"name": "livehost", "url": "http://127.0.0.1:8091", "secret": "s"}
    assert admin_client.post("/v1/plugins", json=body).status_code == 200
    assert admin_client.post("/v1/plugins", json=body).status_code == 409


def test_ticket_is_audience_bound_to_the_named_plugin(user_client):
    _seed()
    r = user_client.post("/v1/plugins/ticket", json={"plugin": "livehost"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["url"] == "http://127.0.0.1:8091"
    assert data["expires_in"] == 60
    assert verify_plugin_token(data["token"], "livehost") is not None
    assert verify_plugin_token(data["token"], "lugo") is None


def test_no_ticket_for_an_unknown_plugin(user_client):
    assert user_client.post("/v1/plugins/ticket", json={"plugin": "nope"}).status_code == 404


def test_no_ticket_for_a_disabled_plugin(user_client):
    _seed(enabled=False)
    assert user_client.post("/v1/plugins/ticket", json={"plugin": "livehost"}).status_code == 404


def test_delete_requires_admin(admin_client, user_client):
    _seed()
    assert user_client.delete("/v1/plugins/livehost").status_code == 403
    assert admin_client.delete("/v1/plugins/livehost").status_code == 200
    assert plugin_store.get("livehost") is None
```

The `admin_client` and `user_client` fixtures do not exist yet as shared fixtures. Before writing this test, read `tests/unit/mcp/` and `tests/unit/http/test_auth_guard.py` to find how those suites authenticate a request, and reuse that exact mechanism — either by importing an existing fixture or by adding these two to `tests/unit/plugins/conftest.py` in the same style. Do not invent a new auth mechanism for tests.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/plugins/test_plugins_routes.py -v`
Expected: FAIL — `KeyError: '/v1/plugins'` on the classification tests, 404 on the route tests.

- [ ] **Step 3: Write the request schemas**

Create `apps/api_gateway/app/schemas/plugins.py`:

```python
from pydantic import BaseModel

from app.services.plugins.models import PluginMount


class PluginRequest(BaseModel):
    name: str
    url: str
    secret: str
    enabled: bool = True
    kind: str = "feature"
    mounts: list[PluginMount] = []


class TicketRequest(BaseModel):
    plugin: str
```

- [ ] **Step 4: Write the routes**

Create `apps/api_gateway/app/api/routes/plugins.py`:

```python
"""Registry of out-of-process plugins, plus the ticket a browser trades for a
direct connection to one.

Shaped after api/routes/mcp.py: same admin-gated write surface, same
user-readable list, same masking of the secret field for non-admin readers.
See specs/2026-08-05-gateway-plugin-contract-design.md.
"""

from fastapi import APIRouter, HTTPException, Request

from app.core.actor import current_role, current_user_id, require_admin
from app.schemas.plugins import PluginRequest, TicketRequest
from app.services.auth.tokens import PLUGIN_TICKET_TTL_SECONDS, issue_plugin_token
from app.services.plugins.models import Plugin
from app.services.plugins.store import plugin_store

router = APIRouter(prefix="/v1/plugins", tags=["plugins"])

_require_admin = require_admin


def _visible(entry: Plugin, user_id: str | None) -> bool:
    return entry.owner_id is None or entry.owner_id == user_id


def _view(entry: Plugin, role: str) -> dict:
    """`secret` authenticates the plugin's introspection calls, so handing it
    to every logged-in user would let any of them resolve any ticket. Masked
    rather than dropped, so the response shape is stable for clients that read
    the field -- same treatment mcp.py gives `headers`."""
    data = entry.model_dump()
    if role != "admin":
        data["secret"] = "***"
    return data


@router.get("")
async def list_plugins(request: Request) -> dict:
    user_id = current_user_id(request)
    role = current_role(request)
    entries = plugin_store.list()
    visible = {k: v for k, v in entries.items() if _visible(v, user_id)}
    return {"success": True, "data": {k: _view(v, role) for k, v in visible.items()}}


@router.post("")
async def add_plugin(payload: PluginRequest, request: Request) -> dict:
    _require_admin(request)
    # exists(), not get() is None: an unreadable row occupies its name and must
    # 409 rather than be silently claimed. Same H4-class check as mcp.py.
    if plugin_store.exists(payload.name):
        raise HTTPException(status_code=409, detail=f"'{payload.name}' already exists")
    entry = Plugin(
        name=payload.name, url=payload.url, secret=payload.secret,
        enabled=payload.enabled, kind=payload.kind, mounts=payload.mounts,
        owner_id=None,
    )
    plugin_store.upsert(entry)
    return {"success": True, "data": entry.model_dump()}


@router.post("/ticket")
async def issue_ticket(payload: TicketRequest, request: Request) -> dict:
    """A short-lived, audience-bound ticket the browser presents to the plugin.

    Fixed path with the plugin in the body, NOT /v1/plugins/{name}/ticket:
    /v1/plugins is an admin prefix and auth_guard._USER_EXACT can only carve a
    user route out of it by exact path and method.
    """
    entry = plugin_store.get(payload.plugin)
    if not entry or not entry.enabled or not _visible(entry, current_user_id(request)):
        raise HTTPException(status_code=404, detail=f"plugin '{payload.plugin}' not found")
    # An unauthenticated caller (auth disabled in dev) has no user_id; the
    # empty string travels as "no owner", matching WsIdentity.unauthenticated.
    user_id = current_user_id(request) or ""
    return {
        "success": True,
        "data": {
            "url": entry.url,
            "token": issue_plugin_token(user_id, payload.plugin),
            "expires_in": PLUGIN_TICKET_TTL_SECONDS,
        },
    }


@router.get("/{name}")
async def get_plugin(name: str, request: Request) -> dict:
    entry = plugin_store.get(name)
    if not entry or not _visible(entry, current_user_id(request)):
        raise HTTPException(status_code=404, detail=f"plugin '{name}' not found")
    return {"success": True, "data": _view(entry, current_role(request))}


@router.put("/{name}")
async def update_plugin(name: str, payload: PluginRequest, request: Request) -> dict:
    _require_admin(request)
    old = plugin_store.get(name)
    if not old:
        raise HTTPException(status_code=404, detail=f"plugin '{name}' not found")
    entry = Plugin(
        name=name, url=payload.url, secret=payload.secret,
        enabled=payload.enabled, kind=payload.kind, mounts=payload.mounts,
        owner_id=old.owner_id,
    )
    plugin_store.upsert(entry)
    return {"success": True, "data": entry.model_dump()}


@router.delete("/{name}")
async def delete_plugin(name: str, request: Request) -> dict:
    _require_admin(request)
    entry = plugin_store.get(name)
    if not entry:
        raise HTTPException(status_code=404, detail=f"plugin '{name}' not found")
    plugin_store.delete(name)
    return {"success": True, "data": {"name": name, "deleted": True}}
```

Route ordering is load-bearing: `POST /v1/plugins/ticket` is declared before `GET|PUT|DELETE /v1/plugins/{name}`, so FastAPI never matches the literal `ticket` as a `{name}` value.

- [ ] **Step 5: Classify the routes**

In `apps/api_gateway/app/core/auth_guard.py`, add `"/v1/plugins"` to `_ADMIN_PREFIXES` (alongside `/v1/quotas`) with this comment:

```python
    # Plugin registry: create/update/delete point the browser at an arbitrary
    # url and hold the secret that authenticates a plugin's introspect calls.
    # Admin-only, with two exact user carve-outs in _USER_EXACT: reading the
    # list (the web client needs it to render feature tabs) and minting a
    # ticket.
    "/v1/plugins",
```

Add both carve-outs to `_USER_EXACT`:

```python
    "/v1/plugins": frozenset({"GET", "HEAD"}),
    # POST only: GET/PUT/DELETE on this exact string dispatch to the
    # `/{name}` handlers with name="ticket", and no POST /v1/plugins/{name}
    # route exists -- so POST here can only ever reach issue_ticket. Same
    # reasoning as /v1/model_registry/options above.
    "/v1/plugins/ticket": frozenset({"POST"}),
```

- [ ] **Step 6: Wire the router**

In `apps/api_gateway/app/main.py`, add the import alongside the others (alphabetical, after `profiles`):

```python
from app.api.routes.plugins import router as plugins_router
```

and the inclusion alongside the others:

```python
app.include_router(plugins_router)
```

- [ ] **Step 7: Run test to verify it passes**

Run: `pytest tests/unit/plugins/ -v`
Expected: PASS

- [ ] **Step 8: Run the route-coverage guard**

Run: `pytest tests/unit/http/ -v`
Expected: PASS. `test_auth_guard_route_coverage.py` walks the live route table; if it fails, the fix is to classify the named path, never to relax the assertion.

- [ ] **Step 9: Full suite and lint**

Run: `pytest -q && ruff check apps/api_gateway/app tests && ruff format --check apps/api_gateway/app tests`
Expected: all pass

- [ ] **Step 10: Commit**

```bash
git add apps/api_gateway/app/api/routes/plugins.py apps/api_gateway/app/schemas/plugins.py apps/api_gateway/app/main.py apps/api_gateway/app/core/auth_guard.py tests/unit/plugins
git commit -m "feat(plugins): registry routes and the ticket a browser trades for a direct connection"
```

**Phase 1 is complete and shippable here.** The gateway has a plugin contract; Livehost is still in-process and untouched.

---

# Phase 2 — `servers/livehost-api`

Phase 2 builds in a **new git repository** at `../livehost-api`, added to the parent as the submodule `servers/livehost-api` in Task 12. Until then it is developed standalone. Commands in Phase 2 run from that repository's root unless stated otherwise.

## Task 5: Scaffold the repository and move the decoupled package

**Files (in the new repo):**
- Create: `pyproject.toml`, `README.md`, `Dockerfile`, `.gitignore`
- Create: `src/livehost/__init__.py`, `src/livehost/cli.py`, `src/livehost/settings.py`
- Move: `src/livehost/schemas.py`, `scheduler.py`, `orchestrator.py`, `registry.py`, `ingest/__init__.py`, `ingest/tiktok.py`
- Move: `tests/test_schemas.py`, `test_ingestor.py`, `test_tiktok_adapter.py`, `test_scheduler.py`, `test_orchestrator.py`

**Interfaces:**
- Produces: the importable package `livehost`, with `livehost.schemas.SocialEvent`, `livehost.scheduler.EventScheduler`, `livehost.scheduler.SocialTurn`, `livehost.orchestrator.LiveHostOrchestrator`, `livehost.orchestrator.format_social_turn`, `livehost.registry.LivehostSession`, `livehost.registry.livehost_registry`, `livehost.ingest.tiktok.TikTokLiveIngestor`, `livehost.ingest.tiktok.RoomOfflineError`, and `livehost.settings.settings`.

- [ ] **Step 1: Create the repository**

```bash
cd /Users/lugon/code
mkdir livehost-api && cd livehost-api && git init
```

- [ ] **Step 2: Copy the five decoupled modules verbatim**

From the gateway repo, copy without editing any logic:

| From `apps/api_gateway/app/services/livehost/` | To `src/livehost/` |
|---|---|
| `schemas.py` | `schemas.py` |
| `scheduler.py` | `scheduler.py` |
| `orchestrator.py` | `orchestrator.py` |
| `registry.py` | `registry.py` |
| `ingestor.py` | `ingest/tiktok.py` (top half) |
| `tiktok_adapter.py` | `ingest/tiktok.py` (bottom half) |

`ingestor.py` and `tiktok_adapter.py` merge into one module because `tiktok_adapter.py` imports `RoomOfflineError` from `ingestor.py` and nothing else imports either separately.

**Integrate the two headers; do not stack them.** A literal concatenation leaves the second file's module docstring sitting mid-file, where it is no longer a docstring at all but a bare string-literal statement — legal Python, invisible to `pydoc`, `help()`, IDE hover and Sphinx. That is precisely where this codebase keeps its reasoning, so stacking would bury it. The second file's imports would likewise strand themselves after class definitions (`E402`).

So: one module docstring at the top carrying the full text of both, nothing reworded or dropped; one import block at the top; the now-internal cross-import removed. Everything below the header keeps every line as-is — no function body, class body, comment or logic line may change, because byte-identity with the originals is the evidence this task exists to produce.

The only edits permitted in this step are import rewrites: `from app.services.livehost.X import Y` becomes `from livehost.X import Y`. If any other change seems necessary, stop — it means the module was not as decoupled as the spec claims, and that is a finding worth reporting before continuing.

Create `src/livehost/ingest/__init__.py` and `src/livehost/__init__.py` as empty files.

- [ ] **Step 3: Copy the five test modules verbatim**

| From gateway `tests/unit/livehost/` | To `tests/` |
|---|---|
| `test_livehost_schemas.py` | `test_schemas.py` |
| `test_livehost_ingestor.py` | `test_ingestor.py` |
| `test_livehost_tiktok_adapter.py` | `test_tiktok_adapter.py` |
| `test_livehost_scheduler.py` | `test_scheduler.py` |
| `test_livehost_orchestrator.py` | `test_orchestrator.py` |

Same rule: import rewrites only.

- [ ] **Step 4: Write `pyproject.toml`**

```toml
[project]
name = "livehost-api"
version = "0.1.0"
description = "TikTok Live AI co-host, as a gateway plugin"
requires-python = ">=3.10"
dependencies = [
  "fastapi",
  "uvicorn[standard]",
  "pydantic>=2",
  "pydantic-settings",
  "httpx",
  "websockets",
  "TikTokLive>=6.6.0",
]

[project.optional-dependencies]
dev = ["pytest", "pytest-asyncio", "pytest-timeout", "ruff"]

[project.scripts]
livehost = "livehost.cli:main"

[tool.pytest.ini_options]
asyncio_mode = "auto"
pythonpath = ["src"]
testpaths = ["tests"]
timeout = 120

[tool.ruff]
line-length = 100

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

`TikTokLive` is a hard dependency here, not an optional extra. Containing it is the point of the move; inside this repository there is nothing to protect from it.

- [ ] **Step 5: Write settings**

Create `src/livehost/settings.py`. These are the eight `livehost_*` fields from the gateway's `core/settings.py`, with their existing defaults, plus what the plugin needs to reach the gateway:

```python
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Where the gateway lives, and how this plugin authenticates to it.
    gateway_url: str = "http://127.0.0.1:8000"
    plugin_name: str = "livehost"
    plugin_secret: str = ""

    host: str = "0.0.0.0"
    port: int = 8091

    # Carried over verbatim from the gateway's core/settings.py.
    mention_keywords: str = ""
    individual_threshold: int = 3
    batch_top_k: int = 3
    queue_max_size: int = 200
    backoff_initial_seconds: float = 1.0
    backoff_max_seconds: float = 60.0
    offline_poll_interval_seconds: float = 30.0
    watchdog_idle_seconds: float = 300.0

    class Config:
        env_prefix = "LIVEHOST_"


settings = Settings()
```

- [ ] **Step 6: Write the CLI**

Create `src/livehost/cli.py`, following `servers/knowledge-api`'s convention that `serve` refuses to start on a failing `doctor`:

```python
"""livehost doctor | serve -- same contract kb uses: serve refuses to start on
a configuration doctor already failed."""

import sys

from livehost.settings import settings


def doctor() -> list[str]:
    """Return a list of problems. Empty means healthy."""
    problems = []
    if not settings.plugin_secret:
        problems.append("LIVEHOST_PLUGIN_SECRET is unset (needed to call /api/auth/introspect)")
    if not settings.gateway_url.startswith(("http://", "https://")):
        problems.append(f"LIVEHOST_GATEWAY_URL is not an http(s) url: {settings.gateway_url!r}")
    try:
        import TikTokLive  # noqa: F401
    except ImportError:
        problems.append("TikTokLive is not installed")
    return problems


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "doctor"
    problems = doctor()
    if cmd == "doctor":
        for p in problems:
            print(f"FAIL  {p}")
        if not problems:
            print("OK    configuration is complete")
        return 1 if problems else 0
    if cmd == "serve":
        if problems:
            for p in problems:
                print(f"FAIL  {p}")
            print("refusing to serve on a failing doctor")
            return 1
        import uvicorn

        from livehost.app import app

        uvicorn.run(app, host=settings.host, port=settings.port)
        return 0
    print(f"unknown command {cmd!r}; expected doctor or serve")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
```

`livehost.app` does not exist until Task 9; it is imported inside the `serve` branch so `doctor` works before then.

- [ ] **Step 7: Run the moved tests**

```bash
pip install -e ".[dev]"
pytest -v
```

Expected: PASS, every moved test green with no edits to its assertions. This is the proof that the boundary was drawn where the code already agreed it was. If any test needs a logic change to pass, stop and report it.

- [ ] **Step 8: Write the Dockerfile and README**

`Dockerfile`, modelled on `servers/knowledge-api/Dockerfile` (read it and match its base image and layer ordering):

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir -e .
EXPOSE 8091
HEALTHCHECK CMD ["livehost", "doctor"]
CMD ["livehost", "serve"]
```

`README.md` covers: what the service is, the four `LIVEHOST_*` environment variables that matter, `livehost doctor`, `livehost serve`, and one paragraph stating that the gateway is a hard runtime dependency.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "feat: the decoupled livehost package, lifted out of the gateway

Five modules and their five tests move with no logic changes -- only import
rewrites. The tests passing unedited is the evidence that this package never
depended on the gateway in the first place."
```

---

## Task 6: Ticket authentication

**Files:**
- Create: `src/livehost/auth.py`
- Test: `tests/test_auth.py`

**Interfaces:**
- Consumes: `settings.gateway_url`, `settings.plugin_name`, `settings.plugin_secret` (Task 5).
- Produces: `async def introspect(ticket: str, client: httpx.AsyncClient | None = None) -> str | None` returning the `user_id` (possibly `""` for an unauthenticated gateway caller) or `None` when the ticket is not active.

- [ ] **Step 1: Write the failing test**

Create `tests/test_auth.py`:

```python
import httpx
import pytest

from livehost.auth import introspect


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_an_active_ticket_returns_its_user_id():
    def handler(request):
        assert request.url.path == "/api/auth/introspect"
        assert request.headers["Authorization"].startswith("Bearer ")
        return httpx.Response(200, json={"success": True,
                                         "data": {"active": True, "user_id": "user-1"}})

    async with _client(handler) as c:
        assert await introspect("tkt", client=c) == "user-1"


async def test_an_inactive_ticket_returns_none():
    def handler(request):
        return httpx.Response(200, json={"success": True,
                                         "data": {"active": False, "user_id": None}})

    async with _client(handler) as c:
        assert await introspect("tkt", client=c) is None


async def test_the_plugin_name_is_sent_so_the_gateway_can_check_the_audience():
    seen = {}

    def handler(request):
        import json
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"success": True,
                                         "data": {"active": True, "user_id": "u"}})

    async with _client(handler) as c:
        await introspect("tkt", client=c)
    assert seen == {"token": "tkt", "plugin": "livehost"}


async def test_a_401_returns_none_rather_than_raising():
    """A rejected plugin secret must close the browser socket, not crash the
    handler and take the TikTok connection down with it."""
    def handler(request):
        return httpx.Response(401, json={"success": False, "error": "invalid"})

    async with _client(handler) as c:
        assert await introspect("tkt", client=c) is None


async def test_a_gateway_outage_returns_none_rather_than_raising():
    def handler(request):
        raise httpx.ConnectError("gateway down")

    async with _client(handler) as c:
        assert await introspect("tkt", client=c) is None


async def test_an_unauthenticated_gateway_caller_yields_an_empty_user_id():
    """Dev mode: the gateway has auth disabled, so the ticket carries "". That
    is a valid identity meaning 'no owner', not a failure."""
    def handler(request):
        return httpx.Response(200, json={"success": True,
                                         "data": {"active": True, "user_id": ""}})

    async with _client(handler) as c:
        assert await introspect("tkt", client=c) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_auth.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'livehost.auth'`

- [ ] **Step 3: Implement**

Create `src/livehost/auth.py`:

```python
"""Trade a browser's ticket for a user id.

One round trip per connection, never on the audio path. The plugin cannot
verify the signature itself: that would mean holding the gateway's session
secret, and anything holding that secret can mint tokens for any user.
"""

from __future__ import annotations

import logging

import httpx

from livehost.settings import settings

logger = logging.getLogger(__name__)


async def introspect(ticket: str, client: httpx.AsyncClient | None = None) -> str | None:
    """Return the user id behind `ticket`, or None if it is not active.

    An empty string is a real, successful answer -- it is what the gateway
    sends when auth is disabled -- so callers must test for None, never for
    falsiness.

    Every failure mode collapses to None on purpose. A gateway outage or a
    rejected plugin secret must close one browser socket, not raise through
    the handler and take a hard-won TikTok connection down with it.
    """
    owned = client is None
    client = client or httpx.AsyncClient()
    try:
        response = await client.post(
            f"{settings.gateway_url}/api/auth/introspect",
            json={"token": ticket, "plugin": settings.plugin_name},
            headers={"Authorization": f"Bearer {settings.plugin_secret}"},
            timeout=5.0,
        )
        if response.status_code != 200:
            logger.warning("introspect rejected: HTTP %s", response.status_code)
            return None
        # Shape-guarded rather than trusted: `response.json()` raises
        # ValueError on a non-JSON body, and `.get()` on a non-dict raises
        # AttributeError. With these isinstance checks neither can arise, so
        # the except clause below stays narrow.
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict) or not data.get("active"):
            return None
        user_id = data.get("user_id")
        return user_id if isinstance(user_id, str) else None
    except (httpx.HTTPError, ValueError) as exc:
        # ValueError covers json.JSONDecodeError. A gateway answering 200 with
        # a body we cannot parse is a hiccup like any other, and the contract
        # is that a hiccup costs one browser socket -- never an exception
        # raised into the handler that owns the TikTok connection.
        logger.warning("introspect failed: %s", exc)
        return None
    finally:
        if owned:
            await client.aclose()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_auth.py -v`
Expected: PASS, 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/livehost/auth.py tests/test_auth.py
git commit -m "feat(auth): trade a ticket for a user id via gateway introspection"
```

---

## Task 7: The upstream conversation client

**Files:**
- Create: `src/livehost/upstream.py`
- Test: `tests/fake_gateway.py`, `tests/test_upstream.py`

**Interfaces:**
- Consumes: `settings.gateway_url` (Task 5).
- Produces:
  - `class Upstream` with `async def connect(self) -> None`, `async def send_audio(self, data: bytes) -> None`, `async def send_text(self, text: str) -> None`, `async def abort(self) -> None`, `async def close(self) -> None`, and `def events(self) -> AsyncIterator[dict | bytes]`.
  - `def build_upstream_url(base: str, token: str, params: dict[str, str]) -> str`.
- The fake gateway in `tests/fake_gateway.py` is reused by Tasks 8 and 9.

- [ ] **Step 1: Write the fake gateway**

Create `tests/fake_gateway.py`. This is the contract's executable specification — it speaks exactly the subset of `/v1/conversation/stream` that the plugin relies on:

```python
"""A stand-in for WS /v1/conversation/stream.

It implements exactly the subset of the real socket that the plugin depends
on. If the real gateway and this fake ever drift apart, the contract has been
broken silently -- which is why the gateway keeps a matching test asserting its
own socket still satisfies the same script.

Control messages in : {"type": "text"|"abort"|"reset"|"new_session"|"flush"|"end"}
Audio frames in     : binary
Events out          : {"event": ..., ...}
Audio out           : binary
"""

import asyncio
import json

from fastapi import FastAPI, WebSocket, WebSocketDisconnect


def build_fake_gateway() -> tuple[FastAPI, dict]:
    """Return (app, log). `log` records everything the plugin sent upstream."""
    app = FastAPI()
    log = {"audio": [], "control": [], "query": {}}

    @app.websocket("/v1/conversation/stream")
    async def stream(websocket: WebSocket):
        await websocket.accept()
        log["query"] = dict(websocket.query_params)
        await websocket.send_json({
            "event": "session_started",
            "session_id": "sess-1",
            "profile": websocket.query_params.get("profile"),
            "stt_engine": "fake-stt",
            "tts_engine": "fake-tts",
            "responder": "fake",
            "sample_rate": 16000,
            "audio_codec": "pcm16",
            "output": ["audio", "text"],
            "audio_out": "wav",
            "output_sample_rate": None,
            "stt_ready": True,
            "tts_ready": True,
        })
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    log["audio"].append(message["bytes"])
                    continue
                if message.get("text") is not None:
                    control = json.loads(message["text"])
                    log["control"].append(control)
                    if control.get("type") == "text":
                        # A turn, condensed: the events the plugin actually keys on.
                        await websocket.send_json({"event": "processing", "turn": 1})
                        await websocket.send_json(
                            {"event": "response_text", "text": f"re: {control['text']}"}
                        )
                        await websocket.send_json({"event": "audio_start"})
                        await websocket.send_bytes(b"AUDIO")
                        await websocket.send_json({"event": "audio_end"})
                        await websocket.send_json({"event": "turn_done", "turn": 1})
                    elif control.get("type") == "abort":
                        await websocket.send_json({"event": "aborted", "reason": "user"})
                    elif control.get("type") == "end":
                        await websocket.send_json({"event": "done"})
                        break
        except WebSocketDisconnect:
            pass

    return app, log


```

**The fake must answer audio, not only text.** A first draft of this file
replied only to `{"type":"text"}` and offered a `speech_start` helper nobody
called — which left the voice-triggered turn, the primary path of a voice
product, with no executable specification anywhere in the contract. Social
turns were specified; the streamer speaking was not.

So the fake also reacts to binary frames, emitting the same sequence the real
socket does when the endpointer fires:

```python
                if message.get("bytes") is not None:
                    log["audio"].append(message["bytes"])
                    # The real socket answers audio with the endpointer's
                    # verdict, then a turn. Livehost's arbitration reads
                    # exactly these two events -- speech_start sets
                    # voice_active, turn_done clears it -- so a fake that
                    # stays silent here specifies away the primary path.
                    await websocket.send_json({"event": "speech_start"})
                    await websocket.send_json({"event": "speech_end"})
                    await websocket.send_json(
                        {"event": "user_transcript", "turn": 1,
                         "text": "hello", "engine": "fake-stt"}
                    )
                    await websocket.send_json({"event": "turn_done", "turn": 1})
                    continue
```

Place this inside the receive loop where the audio branch already logs the
frame, replacing the bare `continue`.

- [ ] **Step 2: Write the failing test**

Create `tests/test_upstream.py`:

```python
import pytest

from livehost.upstream import build_upstream_url


def test_the_url_carries_the_ticket_and_the_requested_modalities():
    url = build_upstream_url(
        "http://gw:8000", "tkt", {"profile": "host", "output": "audio,text"}
    )
    assert url.startswith("ws://gw:8000/v1/conversation/stream?")
    assert "token=tkt" in url
    assert "profile=host" in url
    assert "output=audio%2Ctext" in url


def test_https_becomes_wss():
    url = build_upstream_url("https://gw", "tkt", {})
    assert url.startswith("wss://gw/v1/conversation/stream?")


def test_empty_params_are_dropped_rather_than_sent_blank():
    """A blank ?profile= is not the same as no profile: the gateway would try
    to resolve a profile named "" and warn about it."""
    url = build_upstream_url("http://gw", "tkt", {"profile": "", "voice": None})
    assert "profile=" not in url
    assert "voice=" not in url
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_upstream.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'livehost.upstream'`

- [ ] **Step 4: Implement**

Create `src/livehost/upstream.py`:

```python
"""The plugin's client for WS /v1/conversation/stream.

This is the entire host API for voice. Everything the old in-process handler
did with 21 gateway imports -- STT, endpointing, LLM, TTS with prefetch and
pacing, quota, usage, history, memory injection -- happens on the far side of
this socket.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from urllib.parse import urlencode

import websockets


def build_upstream_url(base: str, token: str, params: dict[str, str | None]) -> str:
    """ws(s):// URL for the conversation socket.

    Blank and None params are dropped rather than sent empty: `?profile=` is
    not the same as no profile -- the gateway would try to resolve a profile
    named "" and emit a warning the browser would then see.
    """
    scheme = "wss" if base.startswith("https://") else "ws"
    host = base.split("://", 1)[-1].rstrip("/")
    query = {k: v for k, v in params.items() if v}
    query["token"] = token
    return f"{scheme}://{host}/v1/conversation/stream?{urlencode(query)}"


class Upstream:
    """One conversation socket, for one browser session."""

    def __init__(self, base: str, token: str, params: dict[str, str | None]) -> None:
        self._url = build_upstream_url(base, token, params)
        self._ws: websockets.WebSocketClientProtocol | None = None

    async def connect(self) -> None:
        self._ws = await websockets.connect(self._url, max_size=None)

    async def send_audio(self, data: bytes) -> None:
        if self._ws is not None:
            await self._ws.send(data)

    async def _control(self, **payload) -> None:
        if self._ws is not None:
            await self._ws.send(json.dumps(payload))

    async def send_text(self, text: str) -> None:
        await self._control(type="text", text=text)

    async def abort(self) -> None:
        await self._control(type="abort")

    async def flush(self) -> None:
        await self._control(type="flush")

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def events(self) -> AsyncIterator[dict | bytes]:
        """Yield upstream traffic: parsed JSON events, raw audio as bytes."""
        if self._ws is None:
            return
        async for message in self._ws:
            if isinstance(message, bytes):
                yield message
            else:
                yield json.loads(message)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_upstream.py -v`
Expected: PASS, 3 passed

- [ ] **Step 6: Commit**

```bash
git add src/livehost/upstream.py tests/test_upstream.py tests/fake_gateway.py
git commit -m "feat(upstream): the conversation socket client, and the fake that specifies it"
```

---

## Task 8: The relay

**Files:**
- Create: `src/livehost/relay.py`
- Test: `tests/test_relay.py`

**Interfaces:**
- Consumes: `Upstream` (Task 7); `LiveHostOrchestrator`, `format_social_turn` (Task 5).
- Produces: `class Relay` with `voice_active: bool`, `async def pump_down(self, send_json, send_bytes) -> None`, `async def pump_up(self, message: dict) -> None`, `async def poll_social(self) -> None`.

The relay is split out from the WS handler so the arbitration logic is testable without a socket on either side. The handler in Task 9 is then thin enough to read in one screen.

- [ ] **Step 1: Write the failing test**

Create `tests/test_relay.py`:

```python
import pytest

from livehost.relay import Relay
from livehost.scheduler import EventScheduler
from livehost.schemas import SocialEvent


class FakeUpstream:
    def __init__(self, events=()):
        self.sent_text = []
        self.sent_audio = []
        self.aborted = 0
        self._events = list(events)

    async def send_text(self, text):
        self.sent_text.append(text)

    async def send_audio(self, data):
        self.sent_audio.append(data)

    async def abort(self):
        self.aborted += 1

    async def events(self):
        for e in self._events:
            yield e


def _relay(upstream, scheduler=None):
    return Relay(upstream=upstream, scheduler=scheduler or EventScheduler())


async def test_speech_start_marks_the_streamer_as_talking():
    upstream = FakeUpstream([{"event": "speech_start"}])
    relay = _relay(upstream)
    sent = []
    await relay.pump_down(sent.append, lambda b: None)
    assert relay.voice_active is True


async def test_turn_done_clears_it():
    upstream = FakeUpstream([{"event": "speech_start"}, {"event": "turn_done", "turn": 1}])
    relay = _relay(upstream)
    await relay.pump_down(lambda e: None, lambda b: None)
    assert relay.voice_active is False


async def test_events_and_audio_are_relayed_verbatim():
    """The browser protocol is unchanged by the port: the gateway's
    session_started is a superset of what livehost used to send, so it passes
    straight through."""
    upstream = FakeUpstream([
        {"event": "session_started", "session_id": "s1", "stt_ready": True},
        b"AUDIO",
        {"event": "turn_done", "turn": 1},
    ])
    relay = _relay(upstream)
    events, audio = [], []
    await relay.pump_down(events.append, audio.append)
    assert events[0]["event"] == "session_started"
    assert events[0]["session_id"] == "s1"
    assert audio == [b"AUDIO"]


async def test_browser_audio_goes_upstream_untouched():
    upstream = FakeUpstream()
    relay = _relay(upstream)
    await relay.pump_up({"bytes": b"MIC"})
    assert upstream.sent_audio == [b"MIC"]


async def test_a_social_turn_is_injected_as_text_when_the_streamer_is_quiet():
    scheduler = EventScheduler()
    scheduler.add(SocialEvent(kind="comment", user_name="ann", text="hi"))
    upstream = FakeUpstream()
    relay = _relay(upstream, scheduler)
    relay.voice_active = False
    await relay.poll_social()
    assert len(upstream.sent_text) == 1
    assert "[TikTok @ann]: hi" in upstream.sent_text[0]


async def test_no_social_turn_while_the_streamer_is_talking():
    scheduler = EventScheduler()
    scheduler.add(SocialEvent(kind="comment", user_name="ann", text="hi"))
    upstream = FakeUpstream()
    relay = _relay(upstream, scheduler)
    relay.voice_active = True
    await relay.poll_social()
    assert upstream.sent_text == []


async def test_barge_in_aborts_the_social_turn():
    """The streamer starting to talk mid-social-turn must cut the co-host off,
    which is what {"type":"abort"} is for."""
    upstream = FakeUpstream([{"event": "speech_start"}])
    relay = _relay(upstream)
    relay.social_turn_in_flight = True
    await relay.pump_down(lambda e: None, lambda b: None)
    assert upstream.aborted == 1
```

Before running, confirm `EventScheduler.add` and `SocialEvent`'s field names against the moved `src/livehost/scheduler.py` and `schemas.py`, and correct the test to match the real signatures. The moved code is the source of truth; do not change it to fit this test.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_relay.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'livehost.relay'`

- [ ] **Step 3: Implement**

Create `src/livehost/relay.py`:

```python
"""Two pumps and an arbiter, with no socket on either side so it can be tested.

Arbitration keeps its old meaning: a social turn only fires when the streamer
is not talking. What changed is where that fact comes from. The endpointer used
to live here; it now lives in the gateway, so `voice_active` is derived from
the upstream event stream instead -- speech_start sets it, turn_done clears it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from livehost.orchestrator import LiveHostOrchestrator
from livehost.scheduler import EventScheduler

logger = logging.getLogger(__name__)


class Relay:
    def __init__(self, upstream, scheduler: EventScheduler) -> None:
        self.upstream = upstream
        self.scheduler = scheduler
        self.orchestrator = LiveHostOrchestrator(scheduler)
        self.voice_active = False
        self.social_turn_in_flight = False

    async def pump_down(
        self,
        send_json: Callable[[dict], Awaitable[None] | None],
        send_bytes: Callable[[bytes], Awaitable[None] | None],
    ) -> None:
        """Upstream to browser. Everything is relayed verbatim; the only local
        work is reading two events for the arbitration state."""
        async for message in self.upstream.events():
            if isinstance(message, bytes):
                await _maybe_await(send_bytes(message))
                continue
            event = message.get("event")
            if event == "speech_start":
                self.voice_active = True
                # The streamer talking over the co-host wins, always.
                if self.social_turn_in_flight:
                    await self.upstream.abort()
                    self.social_turn_in_flight = False
            elif event in ("turn_done", "aborted"):
                self.voice_active = False
                self.social_turn_in_flight = False
            await _maybe_await(send_json(message))

    async def pump_up(self, message: dict) -> None:
        """Browser to upstream. Audio frames and control messages both pass
        through unchanged -- the plugin adds nothing to the voice path, and
        the browser's own abort/flush/end must reach the gateway verbatim."""
        if message.get("bytes") is not None:
            await self.upstream.send_audio(message["bytes"])
            return
        text = message.get("text")
        if text is not None:
            await self.upstream.send_text_raw(text)

    async def poll_social(self) -> None:
        """Fire one social turn if the streamer is quiet and something waits."""
        result = self.orchestrator.poll_social_turn(self.voice_active)
        if result is None:
            return
        _turn, formatted = result
        self.social_turn_in_flight = True
        await self.upstream.send_text(formatted)


async def _maybe_await(value):
    if hasattr(value, "__await__"):
        return await value
    return value
```

`pump_up` needs one method `Upstream` does not have yet. Add it to `src/livehost/upstream.py`, forwarding the browser's raw JSON string through untouched so `abort`, `flush` and `end` arrive exactly as the browser wrote them:

```python
    async def send_text_raw(self, raw: str) -> None:
        if self._ws is not None:
            await self._ws.send(raw)
```

Add the matching stub to `FakeUpstream` in `tests/test_relay.py`:

```python
    async def send_text_raw(self, raw):
        self.sent_raw.append(raw)
```

with `self.sent_raw = []` in its `__init__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_relay.py -v`
Expected: PASS, 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/livehost/relay.py src/livehost/upstream.py tests/test_relay.py
git commit -m "feat(relay): two pumps and an arbiter, with voice_active read off the upstream events"
```

---

## Task 9: The WebSocket handler and control routes

**Files:**
- Create: `src/livehost/app.py`, `src/livehost/api/__init__.py`, `src/livehost/api/ws.py`, `src/livehost/api/control.py`
- Move: `src/livehost/static/livehost.js` and its page
- Test: `tests/test_ws_social.py`, `tests/test_ws_voice.py`, `tests/test_control.py`

**Interfaces:**
- Consumes: `introspect` (Task 6), `Upstream` (Task 7), `Relay` (Task 8), `TikTokLiveIngestor`, `EventScheduler`, `livehost_registry`, `LivehostSession` (Task 5).
- Produces: the ASGI app `livehost.app.app`, serving `WS /v1/livehost/stream`, `POST /v1/livehost/{session_id}/connect`, `POST /v1/livehost/{session_id}/disconnect`, `GET /v1/livehost/{session_id}/status`.

Before writing this task, read the current `apps/api_gateway/app/api/routes/livehost.py` lines 86–130 for the three control endpoints and their ownership check (`_get_owned_session`), and lines 127–330 for the connect-time sequence. The browser-facing wire shape — `{"event": ..., ...}` — must not change; `livehost.js` reads it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_ws_social.py`:

```python
import pytest
from fastapi.testclient import TestClient

from tests.fake_gateway import build_fake_gateway


@pytest.fixture
def gateway(monkeypatch):
    """Point the plugin's upstream at an in-process fake.

    Served on a real loopback port because `websockets.connect` dials a real
    socket; a TestClient-only fake cannot be reached by a real ws client.
    """
    import threading
    import time

    import uvicorn

    app, log = build_fake_gateway()
    config = uvicorn.Config(app, host="127.0.0.1", port=8199, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Bounded wait, not a spin: a server that fails to bind never sets
    # `started`, and a bare `while not server.started: pass` would burn a core
    # until the suite's 120s timeout killed the whole run.
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            server.should_exit = True
            thread.join(timeout=5)
            pytest.fail("fake gateway did not start within 10s")
        time.sleep(0.01)

    monkeypatch.setattr("livehost.settings.settings.gateway_url", "http://127.0.0.1:8199")
    yield log
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def authed(monkeypatch):
    async def _introspect(ticket, client=None):
        return "user-1" if ticket == "good" else None

    monkeypatch.setattr("livehost.api.ws.introspect", _introspect)


def test_a_bad_ticket_is_refused(gateway, authed):
    from livehost.app import app

    with TestClient(app) as client:
        with pytest.raises(Exception):
            with client.websocket_connect("/v1/livehost/stream?ticket=bad"):
                pass


def test_session_started_is_relayed_from_the_gateway(gateway, authed):
    """The plugin does not synthesize this event -- the gateway's version is a
    superset of what livehost used to send, so it passes straight through."""
    from livehost.app import app

    with TestClient(app) as client:
        with client.websocket_connect("/v1/livehost/stream?ticket=good") as ws:
            event = ws.receive_json()
            assert event["event"] == "session_started"
            assert event["stt_engine"] == "fake-stt"
            assert event["stt_ready"] is True


def test_browser_audio_reaches_the_gateway(gateway, authed):
    import time

    from livehost.app import app

    with TestClient(app) as client:
        with client.websocket_connect("/v1/livehost/stream?ticket=good") as ws:
            ws.receive_json()
            ws.send_bytes(b"MIC")
            # Poll to a deadline rather than sleeping a guessed interval: a
            # fixed sleep is either flaky on a loaded machine or slow on an
            # idle one.
            deadline = time.monotonic() + 5
            while b"MIC" not in gateway["audio"] and time.monotonic() < deadline:
                time.sleep(0.01)
    assert b"MIC" in gateway["audio"]
```

Create `tests/test_control.py`:

```python
import pytest
from fastapi.testclient import TestClient

from livehost.app import app


def test_status_for_an_unknown_session_is_404():
    with TestClient(app) as client:
        assert client.get("/v1/livehost/nope/status").status_code == 404


def test_connect_for_an_unknown_session_is_404():
    with TestClient(app) as client:
        r = client.post("/v1/livehost/nope/connect", json={"unique_id": "@someone"})
        assert r.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ws_social.py tests/test_control.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'livehost.app'`

- [ ] **Step 3: Write the WebSocket handler**

Create `src/livehost/api/__init__.py` (empty) and `src/livehost/api/ws.py`:

```python
"""The browser-facing socket.

Four jobs, and none of them is running a conversation turn:
  1. accept, trade the ticket for a user id
  2. open one upstream conversation socket with that user's ticket
  3. relay both directions
  4. poll the social scheduler and inject turns as text

Wire shape to the browser is unchanged from the in-process version --
{"event": ..., ...} -- because livehost.js reads it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from livehost.auth import introspect
from livehost.ingest.tiktok import TikTokLiveIngestor
from livehost.registry import LivehostSession, livehost_registry
from livehost.relay import Relay
from livehost.scheduler import EventScheduler
from livehost.settings import settings
from livehost.upstream import Upstream

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/livehost", tags=["livehost"])

_SOCIAL_POLL_SECONDS = 0.25


def _mention_keywords() -> list[str]:
    return [k.strip() for k in settings.mention_keywords.split(",") if k.strip()]


@router.websocket("/stream")
async def livehost_stream(websocket: WebSocket) -> None:
    ticket = websocket.query_params.get("ticket") or ""
    user_id = await introspect(ticket)
    if user_id is None:
        await websocket.close(code=4401, reason="unauthorized")
        return
    await websocket.accept()

    q = websocket.query_params
    session_id = q.get("session_id") or str(uuid.uuid4())

    upstream = Upstream(
        settings.gateway_url,
        ticket,
        {
            "session_id": q.get("session_id"),
            "profile": q.get("profile"),
            "tts_profile": q.get("tts_profile"),
            "language": q.get("language"),
            "stt_model": q.get("stt_model"),
            "voice": q.get("voice"),
            "sample_rate": q.get("sample_rate"),
            "audio_codec": q.get("audio_codec"),
            "audio_out": q.get("audio_out"),
            "output_sample_rate": q.get("output_sample_rate"),
            "output": q.get("output") or "audio,text",
        },
    )
    try:
        await upstream.connect()
    except Exception as exc:  # noqa: BLE001 - report upstream failure on the wire
        logger.warning("upstream connect failed for %s: %s", session_id, exc)
        await websocket.send_json({"event": "error", "message": "gateway unavailable"})
        await websocket.close()
        return

    scheduler = EventScheduler(
        mention_keywords=_mention_keywords(),
        individual_threshold=settings.individual_threshold,
        batch_top_k=settings.batch_top_k,
        max_queue_size=settings.queue_max_size,
    )
    raw_social_queue: asyncio.Queue = asyncio.Queue()
    ingestor = TikTokLiveIngestor(
        client_factory=_default_tiktok_client_factory,
        queue=raw_social_queue,
        backoff_initial=settings.backoff_initial_seconds,
        backoff_max=settings.backoff_max_seconds,
        offline_poll_interval=settings.offline_poll_interval_seconds,
        watchdog_idle_seconds=settings.watchdog_idle_seconds,
    )
    livehost_registry.register(
        session_id,
        LivehostSession(scheduler=scheduler, ingestor=ingestor, user_id=user_id or None),
    )
    relay = Relay(upstream=upstream, scheduler=scheduler)

    async def _down() -> None:
        await relay.pump_down(websocket.send_json, websocket.send_bytes)

    async def _drain_social() -> None:
        while True:
            event = await raw_social_queue.get()
            scheduler.add(event)

    async def _poll_social() -> None:
        while True:
            await asyncio.sleep(_SOCIAL_POLL_SECONDS)
            await relay.poll_social()

    tasks = [
        asyncio.create_task(_down()),
        asyncio.create_task(_drain_social()),
        asyncio.create_task(_poll_social()),
    ]
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            await relay.pump_up(message)
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()
        await ingestor.stop()
        await upstream.close()
        livehost_registry.unregister(session_id)


def _default_tiktok_client_factory(unique_id: str):
    """Copied verbatim from the gateway's routes/livehost.py."""
    from TikTokLive import TikTokLiveClient

    return TikTokLiveClient(unique_id=unique_id)
```

Copy `_default_tiktok_client_factory` from `apps/api_gateway/app/api/routes/livehost.py:660` exactly rather than from the sketch above, and confirm `TikTokLiveIngestor.stop()` is the real teardown method name in the moved module.

- [ ] **Step 4: Write the control routes**

This is where `test_livehost_authz.py` is reborn. The old ownership check compared `LivehostSession.user_id` against `scope_user_id(request)`; the identity now comes from the caller's ticket instead. Everything else — including 404-not-403, so an unowned session stays indistinguishable from a missing one — is preserved.

Create `src/livehost/api/control.py`:

```python
"""connect / disconnect / status for a live session.

Ported from the gateway's api/routes/livehost.py:86-124. The only change is
where identity comes from: `scope_user_id(request)` became a ticket the caller
presents as a bearer, introspected against the gateway.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from livehost.auth import introspect
from livehost.registry import LivehostSession, livehost_registry

router = APIRouter(prefix="/v1/livehost", tags=["livehost"])


class TikTokConnectRequest(BaseModel):
    unique_id: str


async def _caller_user_id(request: Request) -> str | None:
    scheme, _, ticket = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not ticket.strip():
        return None
    return await introspect(ticket.strip())


async def _get_owned_session(session_id: str, request: Request) -> LivehostSession:
    """404s uniformly for "doesn't exist" and "exists but isn't yours", so this
    is not an existence oracle -- same contract the gateway version kept.

    The comparison is direct, NOT guarded by `scope is not None`, and that
    difference from the gateway's version is the whole point. In the gateway,
    AuthGuardMiddleware has already refused unauthenticated callers before the
    handler runs, so `scope_user_id()` returning None means "admin" or "auth
    disabled server-wide" -- a deliberate bypass. This plugin has no such
    middleware: None here means "presented no ticket, or a bad one". Carrying
    the gateway's guard across that boundary would turn an intentional admin
    bypass into an open door, letting anyone with no credentials at all drive,
    stop or inspect any live session whose id they can guess.
    """
    session = livehost_registry.get(session_id)
    scope = await _caller_user_id(request)
    if session is None or session.user_id != scope:
        raise HTTPException(status_code=404, detail=f"livehost session '{session_id}' not found")
    return session


@router.post("/{session_id}/connect")
async def connect_tiktok(session_id: str, payload: TikTokConnectRequest, request: Request) -> dict:
    session = await _get_owned_session(session_id, request)
    await session.ingestor.start(payload.unique_id)
    return {
        "success": True,
        "data": {"state": session.ingestor.state.value, "unique_id": payload.unique_id},
    }


@router.post("/{session_id}/disconnect")
async def disconnect_tiktok(session_id: str, request: Request) -> dict:
    session = await _get_owned_session(session_id, request)
    await session.ingestor.stop()
    return {"success": True, "data": {"state": session.ingestor.state.value}}


@router.get("/{session_id}/status")
async def livehost_status(session_id: str, request: Request) -> dict:
    session = await _get_owned_session(session_id, request)
    return {
        "success": True,
        "data": {
            "state": session.ingestor.state.value,
            "unique_id": session.ingestor.unique_id,
            "pending_social_events": session.scheduler.pending_count(),
        },
    }
```

Extend `tests/test_control.py` with the ownership cases carried over from `test_livehost_authz.py`:

```python
import pytest
from fastapi.testclient import TestClient

from livehost.app import app
from livehost.registry import LivehostSession, livehost_registry


class _Ingestor:
    state = type("S", (), {"value": "idle"})()
    unique_id = None

    async def start(self, unique_id):
        self.unique_id = unique_id

    async def stop(self):
        pass


class _Scheduler:
    def pending_count(self):
        return 0


@pytest.fixture
def owned(monkeypatch):
    livehost_registry.register(
        "sess-1",
        LivehostSession(scheduler=_Scheduler(), ingestor=_Ingestor(), user_id="user-1"),
    )

    async def _introspect(ticket, client=None):
        return {"tkt-owner": "user-1", "tkt-other": "user-2"}.get(ticket)

    monkeypatch.setattr("livehost.api.control.introspect", _introspect)
    yield
    livehost_registry.unregister("sess-1")


def _get(client, ticket):
    return client.get("/v1/livehost/sess-1/status",
                      headers={"Authorization": f"Bearer {ticket}"})


def test_the_owner_can_read_status(owned):
    with TestClient(app) as client:
        assert _get(client, "tkt-owner").status_code == 200


def test_another_users_session_is_404_not_403(owned):
    """Not 403: an unowned session must be indistinguishable from a missing
    one, or the id space becomes an existence oracle."""
    with TestClient(app) as client:
        assert _get(client, "tkt-other").status_code == 404


def test_a_missing_ticket_cannot_drive_another_users_session(owned):
    with TestClient(app) as client:
        r = client.post("/v1/livehost/sess-1/connect", json={"unique_id": "@x"})
        assert r.status_code == 404


def test_status_for_an_unknown_session_is_404():
    with TestClient(app) as client:
        assert client.get("/v1/livehost/nope/status").status_code == 404


def test_connect_for_an_unknown_session_is_404():
    with TestClient(app) as client:
        r = client.post("/v1/livehost/nope/connect", json={"unique_id": "@someone"})
        assert r.status_code == 404
```

This replaces the smaller `tests/test_control.py` sketched in Step 1. Before running, check `_Ingestor`/`_Scheduler` against the real `TikTokLiveIngestor` and `EventScheduler` signatures in the moved modules and adjust the stubs, not the modules.

- [ ] **Step 5: Write the ASGI app**

Create `src/livehost/app.py`:

```python
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from livehost.api.control import router as control_router
from livehost.api.ws import router as ws_router

app = FastAPI(title="livehost-api")
app.include_router(ws_router)
app.include_router(control_router)
app.mount("/static", StaticFiles(directory="src/livehost/static"), name="static")
```

- [ ] **Step 6: Move the UI**

**Topology, decided here because two tasks depend on it:** the plugin serves its own page at `<plugin url>/ui`, and the gateway's `/ui` renders a tab that opens it. That is what actually gets `livehost.js` out of the gateway in Task 13. The consequence is that the plugin's page calls the gateway cross-origin, which is exactly the cost accepted when choosing direct browser-to-plugin connections — and it is configuration, not code, because the gateway already runs `CORSMiddleware` with `settings.cors_origins_list` (Task 12 Step 2 sets it).

Copy `apps/api_gateway/app/static/js/livehost.js` to `src/livehost/static/livehost.js`, and extract the livehost page out of `apps/api_gateway/app/static/index.html` into `src/livehost/static/index.html`. Add a `GET /ui` route to `src/livehost/app.py` returning that file, mirroring the gateway's `api/routes/ui.py`.

Three changes in the JS, and no others:

1. The WebSocket URL comes from the ticket response (`data.url`) plus the mount path plus `?ticket=`, instead of a same-origin path.
2. Any call the page makes to the gateway — `POST /v1/plugins/ticket`, and the profile/TTS-profile dropdown feeds `GET /v1/profiles` and `GET /v1/tts_profiles` — is now absolute against the gateway origin and sends the user's bearer token. Grep the file for `fetch(` to find them all.
3. The three control calls (`connect`, `disconnect`, `status`) go to the plugin's own origin and carry `Authorization: Bearer <ticket>`, which is what `_get_owned_session` introspects.

Everything the JS does with `{"event": ...}` messages stays as it is — the gateway's `session_started` is a superset of the old one.

- [ ] **Step 7: Run the tests**

Run: `pytest -v`
Expected: PASS, including the five moved tests from Task 5.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat(ws): the browser socket, relaying to one upstream conversation per session"
```

---

## Task 10: Upstream reconnection

**Files:**
- Modify: `src/livehost/api/ws.py`
- Test: `tests/test_upstream_reconnect.py`

**Interfaces:**
- Consumes: `Upstream`, `Relay`.
- Produces: no new public names; `_down()` gains a reconnect loop.

The TikTok connection is expensive to establish and carries its own backoff. An upstream drop must not take it down.

- [ ] **Step 1: Write the failing test**

Create `tests/test_upstream_reconnect.py`:

```python
import asyncio

import pytest


class FlakyUpstream:
    """Drops once, then behaves."""

    def __init__(self):
        self.connects = 0
        self.session_id = None
        self._dropped = False

    async def connect(self):
        self.connects += 1

    async def events(self):
        if not self._dropped:
            self._dropped = True
            yield {"event": "session_started", "session_id": "sess-1"}
            raise ConnectionError("upstream dropped")
        yield {"event": "session_started", "session_id": "sess-1"}
        await asyncio.sleep(0.05)

    async def close(self):
        pass


async def test_the_upstream_is_redialled_after_a_drop():
    from livehost.api.ws import relay_with_reconnect

    upstream = FlakyUpstream()
    events = []

    async def run():
        await relay_with_reconnect(upstream, events.append, lambda b: None, max_attempts=2)

    await asyncio.wait_for(run(), timeout=5)
    assert upstream.connects == 2


async def test_the_session_id_is_carried_across_the_reconnect():
    """History must stay continuous: the gateway resumes the same stored
    session when ?session_id= names one it already owns."""
    from livehost.api.ws import resume_params

    params = resume_params({"profile": "host"}, session_id="sess-1")
    assert params["session_id"] == "sess-1"
    assert params["profile"] == "host"


async def test_the_ingestor_is_never_torn_down_by_an_upstream_drop():
    """A TikTok connection costs backoff and time to rebuild; an upstream
    hiccup must not spend it."""
    from livehost.api.ws import relay_with_reconnect

    upstream = FlakyUpstream()
    stopped = []

    class Ingestor:
        async def stop(self):
            stopped.append(True)

    await asyncio.wait_for(
        relay_with_reconnect(upstream, lambda e: None, lambda b: None, max_attempts=2),
        timeout=5,
    )
    assert stopped == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_upstream_reconnect.py -v`
Expected: FAIL with `ImportError: cannot import name 'relay_with_reconnect'`

- [ ] **Step 3: Implement**

Add to `src/livehost/api/ws.py`:

```python
def resume_params(params: dict, session_id: str | None) -> dict:
    """Upstream query params for a reconnect. session_id is what keeps the
    gateway writing to the same stored session, so history does not fork."""
    resumed = dict(params)
    if session_id:
        resumed["session_id"] = session_id
    return resumed


async def relay_with_reconnect(upstream, send_json, send_bytes, max_attempts: int = 5) -> None:
    """Relay downstream, redialling the gateway when it drops.

    The ingestor is deliberately NOT in scope here: a TikTok connection costs
    backoff and time to rebuild, and an upstream hiccup must not spend it.
    Social events keep accumulating in the scheduler's bounded queue during the
    gap and are subject to its existing overflow behaviour.
    """
    attempt = 0
    while attempt < max_attempts:
        attempt += 1
        try:
            await upstream.connect()
            async for message in upstream.events():
                if isinstance(message, bytes):
                    await _maybe_await(send_bytes(message))
                else:
                    await _maybe_await(send_json(message))
            return
        except (ConnectionError, OSError) as exc:
            logger.warning("upstream dropped (attempt %d/%d): %s", attempt, max_attempts, exc)
            await asyncio.sleep(min(2 ** (attempt - 1), 10))
    logger.error("giving up on the upstream after %d attempts", max_attempts)
```

Import `_maybe_await` from `livehost.relay`, and rewire the handler's `_down()` to call `relay_with_reconnect` through the `Relay` so `voice_active` bookkeeping still runs. Concretely: give `Relay.pump_down` an optional `reconnect` callable, or move the reconnect loop to wrap `relay.pump_down` — choose whichever keeps `Relay`'s tests from Task 8 passing unchanged, and say which you chose in the commit message.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_upstream_reconnect.py -v`
Expected: PASS, 3 passed

- [ ] **Step 5: Run the whole suite**

Run: `pytest -q && ruff check src tests`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "fix(upstream): redial the gateway without dropping the TikTok connection"
```

---

# Phase 3 — Cutover

## Task 11: Retarget the three gateway-side tests

**Files (gateway repo):**
- Delete: `tests/unit/livehost/test_livehost_quota_gate.py` (duplicate coverage; one replacement test added below)
- Modify: `tests/unit/conversation/test_turn_quota.py` (add `test_an_injected_text_turn_is_quota_gated`)
- Rewrite: `tests/unit/livehost/test_livehost_tts_profile.py` → `tests/unit/conversation/test_conversation_tts_profile.py`
- Rewrite: `tests/integration/test_livehost_disabled_cutoff.py` → `tests/integration/test_conversation_disabled_cutoff.py`

**Interfaces:** none — this task only moves guarantees onto the socket that will own them.

These three build the gateway app directly and stub STT/TTS providers, so the behaviour they guard becomes `conversation/stream` behaviour after the port. **This task runs before Task 12 so the guarantees are never unguarded, not even for one commit.**

They do not all deserve the same treatment. `test_livehost_quota_gate.py` turns out to be mostly duplicate coverage; the other two are genuine and must move.

- [ ] **Step 1: Retire the quota test, and replace what it uniquely held**

`test_livehost_quota_gate.py` holds three tests. Two of them — `test_livehost_quota_helper_blocks_when_over_limit` and `test_livehost_quota_helper_fails_open` — exercise `_quota_blocked_for`, which is a thin wrapper over `llm_turn_quota_blocked_for_pins`. That shared helper is already covered directly by `tests/unit/conversation/test_turn_quota.py::test_llm_turn_quota_blocked_over_limit_via_profile` and `::test_llm_turn_quota_blocked_fails_open_on_gate_error`. Confirm those two exist and pass, then let the livehost pair go.

The third, `test_livehost_module_gates_its_turns`, is a structural check: it reads the livehost route module's source and asserts both turn functions reach the gate. It becomes vacuous once livehost runs no turns. Its guarantee moves to the gateway by construction — `ConversationSession._run_turn` gates once per turn *above* the audio/text branch, so an injected social turn cannot bypass it — but nothing asserts that for the text path specifically, and the text path is precisely what the plugin now depends on.

Add that one test to `tests/unit/conversation/test_turn_quota.py`:

```python
@pytest.mark.asyncio
async def test_an_injected_text_turn_is_quota_gated():
    """The livehost plugin drives social turns with {"type":"text"}. The gate
    sits above _run_turn's audio/text branch, so text is covered by
    construction -- this pins that construction down, because the plugin has
    no gate of its own any more."""
    import inspect

    from app.services.conversation import session as session_module

    source = inspect.getsource(session_module.ConversationSession._run_turn)
    gate_at = source.index("llm_turn_quota_blocked")
    text_branch_at = source.index("if text_input is not None")
    assert gate_at < text_branch_at, (
        "the quota gate must run before _run_turn splits into the audio and "
        "text paths, or an injected social turn skips it"
    )
```

Then delete the file:

```bash
git rm tests/unit/livehost/test_livehost_quota_gate.py
pytest tests/unit/conversation/test_turn_quota.py -v
```

Expected: PASS, including the new test.

- [ ] **Step 2: Check the targets before writing anything**

Both files this task was going to create **already exist**, and have since long before this plan:

- `tests/unit/conversation/test_conversation_tts_profile.py` — four tests, already against `conversation/stream`
- `tests/integration/test_conversation_disabled_cutoff.py` — one test, `test_disabled_user_connection_is_closed_within_recheck_interval`

So most of what looked like retargeting is really deduplication. Build the mapping before touching anything:

| livehost test | conversation equivalent |
|---|---|
| `test_livehost_tts_profile_linked_from_llm_profile` | `test_tts_profile_linked_from_llm_profile_resolves_clone_fields` |
| `test_livehost_query_param_tts_profile_overrides_llm_profile` | `test_query_param_tts_profile_overrides_llm_profile` |
| `test_livehost_no_tts_profile_falls_back_to_default_tts_engine` | `test_no_tts_profile_falls_back_to_default_tts_engine` |
| `test_livehost_bad_ref_audio_path_degrades_to_tts_error` | **none — this one is unique** |
| `test_disabled_user_connection_is_closed_within_recheck_interval` (integration) | identically-named twin already present |

Confirm each pairing by reading both tests, not by matching names. A same-named test can assert a different thing.

- [ ] **Step 3: Preserve the one guarantee that has no twin**

`test_livehost_bad_ref_audio_path_degrades_to_tts_error` is the only guarantee in these two files with no conversation-side equivalent: a TTS profile pointing at a missing reference-audio file must degrade to a reported TTS error rather than killing the turn. Deleting the file without moving this test would silently drop it — which is the exact failure this task exists to prevent.

Add it to the existing `tests/unit/conversation/test_conversation_tts_profile.py`, keeping its assertion and provider stubs verbatim; only the socket changes to `/v1/conversation/stream`, and a turn is driven with `{"type": "text", "text": "hello"}` instead of an injected TikTok event.

Run: `pytest tests/unit/conversation/test_conversation_tts_profile.py -v`
Expected: PASS, five tests — the four that were already there plus this one.

Any guarantee that cannot be made to hold against `conversation/stream` is a real finding: it would mean the gateway's socket does not provide something livehost was relying on. Stop and report it rather than dropping the test.

- [ ] **Step 4: Delete the now-duplicated originals and run the full suite**

```bash
git rm tests/unit/livehost/test_livehost_tts_profile.py \
       tests/integration/test_livehost_disabled_cutoff.py
pytest -q
```

Expected: PASS. The suite count should fall by four, not five: five livehost tests go, one is re-added on the conversation socket.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/conversation tests/integration
git commit -m "test(conversation): move livehost's guarantees onto the socket that will own them

TTS profile precedence and the mid-session disable cutoff are conversation/
stream behaviour once livehost is a plugin, so they move there. The quota
tests turn out to be duplicates of test_turn_quota.py's coverage of the shared
helper, and the structural one asserted a wiring that stops existing -- what
replaces them is a single test pinning the gate above _run_turn's audio/text
branch, since an injected social turn is the path the plugin now depends on.

Retargeted before the removal, so nothing is unguarded even for one commit."
```

---

## Task 12: Register the plugin and point the UI at it

**Files (gateway repo):**
- Modify: `apps/api_gateway/app/static/js/app.js` or whichever module renders the feature tabs (find it by grepping for the livehost tab)
- Modify: `.gitmodules` (add `servers/livehost-api`)
- Create: `docs/` note or `README.md` section on registering a plugin

- [ ] **Step 1: Add the submodule**

```bash
cd /Users/lugon/code/speech-text-transformer
git submodule add ../livehost-api servers/livehost-api
```

- [ ] **Step 2: Allow the plugin's origin through CORS**

The plugin's page calls the gateway cross-origin (Task 9 Step 6). The gateway already runs `CORSMiddleware` with `allow_origins=settings.cors_origins_list`, deliberately outermost so it also wraps `AuthGuardMiddleware`'s 401/403 — `tests/integration/test_cors_ordering.py` and `test_cors_bearer.py` enforce that ordering, so do not move it.

Add the plugin's origin (`http://127.0.0.1:8091` in development) to the setting that feeds `cors_origins_list`. Read `apps/api_gateway/app/core/settings.py` for the field's exact name and separator convention, and set it through the environment rather than editing the default.

Run: `pytest tests/integration/test_cors_ordering.py tests/integration/test_cors_bearer.py -v`
Expected: PASS — unchanged, since this is configuration.

- [ ] **Step 3: Register the plugin against a running gateway**

```bash
curl -X POST localhost:8000/v1/plugins \
  -H "Authorization: Bearer <admin-token>" -H 'Content-Type: application/json' \
  -d '{"name":"livehost","url":"http://127.0.0.1:8091","secret":"<shared-secret>",
       "mounts":[{"path":"/v1/livehost/stream","kind":"ws"}]}'
```

Set the same value as `LIVEHOST_PLUGIN_SECRET` in the plugin's environment.

- [ ] **Step 4: Make the gateway UI discover plugins**

Find the code that renders the livehost tab (`grep -rn "livehost" apps/api_gateway/app/static/`). Replace the hardcoded tab with a generic one: read `GET /v1/plugins` on load and render a tab for each enabled plugin whose `kind` is `"feature"`, opening `<plugin url>/ui`.

The tab is generic on purpose — it is what lets Lugo become the second plugin without touching the UI again.

- [ ] **Step 5: Verify end to end by hand**

Start the gateway, start `livehost serve`, open the gateway UI, click through to the livehost tab, connect to a TikTok room, and confirm all four legs:

1. A viewer comment produces spoken audio in the browser.
2. Talking over the co-host cuts it off (barge-in reaches `{"type":"abort"}`).
3. The profile and TTS-profile dropdowns populate — that is the cross-origin `GET /v1/profiles` working.
4. `livehost doctor` reports OK.

Record the result in the commit message. If any leg fails, fix it before proceeding — Task 13 is irreversible.

- [ ] **Step 6: Commit**

```bash
git add .gitmodules servers/livehost-api apps/api_gateway/app/static
git commit -m "feat(ui): discover feature plugins through GET /v1/plugins"
```

---

## Task 13: Remove Livehost from the gateway

**Files (gateway repo):**
- Delete: `apps/api_gateway/app/services/livehost/`, `apps/api_gateway/app/api/routes/livehost.py`, `apps/api_gateway/app/schemas/livehost.py`, `apps/api_gateway/app/static/js/livehost.js`, `tests/unit/livehost/`, `tests/integration/test_livehost_ws_social.py`, `tests/integration/test_livehost_ws_voice.py`
- Modify: `apps/api_gateway/app/main.py`, `apps/api_gateway/app/core/auth_guard.py`, `apps/api_gateway/app/core/settings.py`, `pyproject.toml`

**This is the only irreversible step.** Everything before it is additive.

- [ ] **Step 1: Confirm the replacement works**

Re-run the four-leg manual check from Task 12 Step 5. Do not proceed on a failing leg.

- [ ] **Step 2: Remove the wiring**

In `apps/api_gateway/app/main.py`, delete the `livehost_router` import and its `include_router` call.

In `apps/api_gateway/app/core/auth_guard.py`, delete `"/v1/livehost",` from `_USER_PREFIXES`.

In `apps/api_gateway/app/core/settings.py`, delete the eight `livehost_*` fields (`livehost_mention_keywords`, `livehost_individual_threshold`, `livehost_batch_top_k`, `livehost_queue_max_size`, `livehost_backoff_initial_seconds`, `livehost_backoff_max_seconds`, `livehost_offline_poll_interval_seconds`, `livehost_watchdog_idle_seconds`) and their comment block.

In `pyproject.toml`, delete the `tiktok` extra and its comment.

- [ ] **Step 3: Delete the code and tests**

```bash
git rm -r apps/api_gateway/app/services/livehost \
         apps/api_gateway/app/api/routes/livehost.py \
         apps/api_gateway/app/schemas/livehost.py \
         apps/api_gateway/app/static/js/livehost.js \
         tests/unit/livehost \
         tests/integration/test_livehost_ws_social.py \
         tests/integration/test_livehost_ws_voice.py
```

Remove the livehost page markup from `apps/api_gateway/app/static/index.html`.

- [ ] **Step 4: Find every straggler**

Run: `grep -rn "livehost" apps/ tests/ pyproject.toml --include="*.py" --include="*.html" --include="*.js" --include="*.toml"`

Expected: only prose references in comments that explain shared helpers (`turn_quota.py`, `turn_tts.py`, `tts_params.py`, `endpointer.py`, `attribution.py`, `profile_visibility.py`, `pairing.py`, `opus.py`, `session.py`, `sessions.py`, `conversation.py`, `lugo.py`). Those comments name livehost as a second consumer of a shared helper. Update each to say the consumer is now the livehost plugin, reached over `conversation/stream` — do not delete the comments, they explain why the helper was extracted.

- [ ] **Step 5: Run the full suite**

Run: `pytest -q && ruff check apps/api_gateway/app tests && ruff format --check apps/api_gateway/app tests`
Expected: PASS. The route-coverage guard confirms no orphaned `/v1/livehost` classification remains.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor: livehost leaves the gateway

The router, the service package, the schemas, the static JS, eight settings
fields and the TikTok extra all go. What stays is the conversation socket the
plugin now speaks to -- and the comments explaining which shared helpers have
a second consumer, updated to say where that consumer moved."
```

---

## Out of scope

**Lugo.** `api/routes/lugo.py` is 654 lines with 16 gateway imports — the same shape as livehost, and the second consumer the contract was designed against. It gets its own spec once this contract has carried real traffic.

**Multi-replica livehost.** `livehost_registry` is a process-global dict and the three control endpoints resolve sessions through it. Running more than one replica breaks them. This is recorded in the spec's *Known limitations*; do not paper over it with sticky routing that is not asked for.
