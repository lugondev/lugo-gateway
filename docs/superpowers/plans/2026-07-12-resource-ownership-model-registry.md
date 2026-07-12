# Resource Ownership & Model Registry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Scope Profile/TtsProfile/McpServer/ChatSession/Memory to their owning user (closing the "any registered user can read/edit everyone else's MCP secrets and chat history" gap), and add an admin-managed model registry that gates STT/TTS/LLM choices by `enabled`/`stage=testing`.

**Architecture:** `Profile`/`TtsProfile`/`McpServer` are JSON-blob rows (`SqliteBackedStore`) — adding `owner_id` is a pure Pydantic field addition, no schema migration. `ChatSession`/`MemoryItem`/`MemoryProfileDoc` are real SQL tables already populated in existing databases, so `user_id` needs a small idempotent `ALTER TABLE ADD COLUMN` helper run at startup (this codebase has no Alembic). A new `model_registry_entries` table gates STT/TTS/LLM model choices; a new admin-only creation endpoint runs a real test call against the chosen engine/model before persisting an entry, reusing existing provider/responder code.

**Tech Stack:** FastAPI, SQLAlchemy (async engine, SQLite), the existing `SqliteBackedStore` JSON-blob pattern, pytest + pytest-asyncio.

## Global Constraints

- No DB schema change for `Profile`/`TtsProfile`/`McpServer` ownership — `owner_id` is a plain Pydantic field on the existing JSON-blob-backed models.
- `ChatSession`/`MemoryItem`/`MemoryProfileDoc` need a real, idempotent `ALTER TABLE ADD COLUMN user_id` migration at startup (no Alembic in this repo) — `Base.metadata.create_all` does not alter existing tables.
- Clones are fully independent copies from the moment of creation — no live link back to the source template, no "reset to template."
- A model/engine choice that matches *no* registry entry is never blocked — the registry only exercises authority over entries an admin explicitly catalogued; self-hosted/custom LLM endpoints stay unrestricted.
- Manually adding a registry entry (`POST /v1/model_registry`) must run a real test call against the chosen provider/engine *before* the row is persisted, and reject (400) if it fails. Auto-seeded entries (from already-installed STT/TTS engines) are not re-tested at seed time.
- Every new/changed backend behavior gets a pytest test in `tests/unit/` or `tests/integration/`.
- Run all commands from the repo root (`/Users/lugon/code/speech-text-transformer/.worktrees/identity-auth-device-pairing`); use `/Users/lugon/code/speech-text-transformer/.venv/bin/python -m pytest tests/unit tests/integration -q` (not bare `pytest -q` — some root-level `tests/*.py` files make real network calls and can hang in this sandbox, unrelated to this branch).

---

## Task 1: shared actor helper + `Profile` ownership (owner_id, scoped routes, clone)

**Files:**
- Create: `apps/api_gateway/app/core/actor.py`
- Test: `tests/unit/test_actor.py`
- Modify: `apps/api_gateway/app/services/profiles/models.py`
- Modify: `apps/api_gateway/app/api/routes/profiles.py`
- Test: `tests/unit/test_profile_ownership.py`

**Interfaces:**
- Consumes: `request.session` (already populated by the auth system when auth is enabled).
- Produces: `current_user_id(request) -> str | None` and `current_role(request) -> str` (`app.core.actor`, reused by every task in this plan that needs the acting identity) — critically, both are safe to call even when `settings.admin_password` is unset (dev mode, `AuthGuardMiddleware` no-ops and the route runs with an empty session): `current_role` falls back to `"admin"` (unrestricted, matching today's pre-ownership behavior when auth is off) instead of raising `KeyError` on a missing session key. This mirrors a real bug caught in the identity/auth branch's final review (`request.session["user_id"]` direct-subscript crashing with 500 instead of a clean fallback) — don't repeat it here.
- Produces: `Profile.owner_id: str | None`; route behavior — `GET /v1/profiles` returns only templates (`owner_id is None`) plus the caller's own rows; `POST /v1/profiles` sets `owner_id = None` for an admin caller, `owner_id = <user_id>` otherwise; `GET/PUT/DELETE /v1/profiles/{name}` 404 on a row that exists but isn't visible to the caller; new `POST /v1/profiles/{name}/clone {new_name}`.

- [ ] **Step 0: Write the actor helper first (used by every route change below)**

```python
# tests/unit/test_actor.py
from starlette.requests import Request

from app.core.actor import current_role, current_user_id


class _FakeRequest:
    def __init__(self, session: dict):
        self.session = session


def test_current_role_defaults_to_admin_when_session_empty():
    assert current_role(_FakeRequest({})) == "admin"


def test_current_role_returns_actual_role_when_present():
    assert current_role(_FakeRequest({"role": "user"})) == "user"


def test_current_user_id_returns_none_when_absent():
    assert current_user_id(_FakeRequest({})) is None


def test_current_user_id_returns_value_when_present():
    assert current_user_id(_FakeRequest({"user_id": "u1"})) == "u1"
```

Run: `pytest tests/unit/test_actor.py -v` — expected to FAIL (`ModuleNotFoundError`), then implement:

```python
# apps/api_gateway/app/core/actor.py
"""Safe accessors for the acting identity on a request. Both fall back
gracefully when settings.admin_password is unset (dev mode): AuthGuardMiddleware
no-ops for every prefix in that case, so a route can run with a completely
empty session -- request.session["role"] would raise KeyError there. Treating
a missing role as "admin" matches today's actual dev-mode behavior (a single
unauthenticated caller has unrestricted access) rather than crashing."""

from starlette.requests import Request


def current_user_id(request: Request) -> str | None:
    return request.session.get("user_id")


def current_role(request: Request) -> str:
    return request.session.get("role") or "admin"
```

Run: `pytest tests/unit/test_actor.py -v` — expected: 4 passed. Commit this step with the rest of Task 1 (Step 5 below covers both).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_profile_ownership.py
import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _signup_login(client, username: str, role: str = "user") -> None:
    client.post("/api/auth/signup", json={"username": username, "password": "pw"})
    if role == "admin":
        import asyncio

        from app.services.auth.users import user_store

        user = asyncio.run(user_store.get_by_username(username))
        asyncio.run(user_store.set_fields(user.id, role="admin"))
    client.post("/api/auth/login", json={"username": username, "password": "pw"})


def _minimal_profile(name: str) -> dict:
    return {"name": name}


def test_admin_created_profile_is_a_template(client, _with_password):
    _signup_login(client, "root", role="admin")
    resp = client.post("/v1/profiles", json=_minimal_profile("shared"))
    assert resp.status_code == 200
    assert resp.json()["data"]["owner_id"] is None


def test_user_created_profile_is_owned(client, _with_password):
    _signup_login(client, "toan", role="user")
    resp = client.post("/v1/profiles", json=_minimal_profile("mine"))
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["owner_id"] is not None


def test_list_shows_templates_and_own_but_not_others(client, _with_password):
    _signup_login(client, "root", role="admin")
    client.post("/v1/profiles", json=_minimal_profile("template-a"))

    _signup_login(client, "a", role="user")
    client.post("/v1/profiles", json=_minimal_profile("a-private"))

    _signup_login(client, "b", role="user")
    client.post("/v1/profiles", json=_minimal_profile("b-private"))
    names = set(client.get("/v1/profiles").json()["data"].keys())
    assert names == {"template-a", "b-private"}  # sees the template + own, not a's


def test_get_other_users_private_profile_is_404(client, _with_password):
    _signup_login(client, "a", role="user")
    client.post("/v1/profiles", json=_minimal_profile("a-private"))

    _signup_login(client, "b", role="user")
    resp = client.get("/v1/profiles/a-private")
    assert resp.status_code == 404


def test_clone_template_creates_owned_copy(client, _with_password):
    _signup_login(client, "root", role="admin")
    client.post("/v1/profiles", json=_minimal_profile("template-a"))

    _signup_login(client, "toan", role="user")
    resp = client.post("/v1/profiles/template-a/clone", json={"new_name": "toan-copy"})
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["name"] == "toan-copy"
    assert body["owner_id"] is not None
    # confirm it is now independently listed/owned
    names = set(client.get("/v1/profiles").json()["data"].keys())
    assert "toan-copy" in names


def test_clone_nonexistent_or_invisible_is_404(client, _with_password):
    _signup_login(client, "a", role="user")
    client.post("/v1/profiles", json=_minimal_profile("a-private"))

    _signup_login(client, "b", role="user")
    resp = client.post("/v1/profiles/a-private/clone", json={"new_name": "steal"})
    assert resp.status_code == 404


def test_clone_name_collision_is_409(client, _with_password):
    _signup_login(client, "root", role="admin")
    client.post("/v1/profiles", json=_minimal_profile("template-a"))

    _signup_login(client, "toan", role="user")
    client.post("/v1/profiles", json=_minimal_profile("taken"))
    resp = client.post("/v1/profiles/template-a/clone", json={"new_name": "taken"})
    assert resp.status_code == 409
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_profile_ownership.py -v`
Expected: FAIL — `owner_id` key doesn't exist yet, no scoping, no clone route (404 on the clone URL)

- [ ] **Step 3: Write the implementation**

In `apps/api_gateway/app/services/profiles/models.py`, add to `Profile` (after `name`):

```python
    owner_id: str | None = None
```

Replace `apps/api_gateway/app/api/routes/profiles.py`:

```python
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core.actor import current_role, current_user_id
from app.core.errors import AppError
from app.services.mcp.models import McpServer
from app.services.profiles.models import LlmConfig, MemoryConfig, Profile, SessionConfig, SttConfig, TtsConfig
from app.services.profiles.store import profile_store
from app.services.stt.model_registry import STT_MODEL_REGISTRIES
from app.services.stt.profile import resolve_stt_profile

router = APIRouter(prefix="/v1/profiles", tags=["profiles"])


def _mask(profile: Profile) -> dict:
    data = profile.model_dump()
    if data.get("llm", {}).get("api_key"):
        data["llm"]["api_key"] = "***"
    return data


def _validate_stt_model(profile: Profile) -> None:
    if not profile.stt.model:
        return
    preset = resolve_stt_profile(profile.stt.profile)
    engine = profile.stt.engine or (preset[0] if preset else "")
    if not engine:
        raise AppError("stt.model requires stt.engine or a resolvable stt.profile preset")
    registry = STT_MODEL_REGISTRIES.get(engine)
    if registry is None:
        raise AppError(f"engine '{engine}' has no selectable model variants")
    registry.validate(profile.stt.model)


def _visible(profile: Profile, user_id: str | None) -> bool:
    return profile.owner_id is None or profile.owner_id == user_id


class ProfileRequest(BaseModel):
    name: str
    nickname: str = ""
    llm: LlmConfig = LlmConfig()
    system_prompt: str = ""
    voice_optimized: bool = False
    stt: SttConfig = SttConfig()
    tts: TtsConfig = TtsConfig()
    mcp_servers: list[McpServer] = []
    memory: MemoryConfig = MemoryConfig()
    session: SessionConfig = SessionConfig()


class CloneRequest(BaseModel):
    new_name: str


@router.get("")
async def list_profiles(request: Request) -> dict:
    user_id = current_user_id(request)
    profiles = profile_store.list()
    visible = {k: v for k, v in profiles.items() if _visible(v, user_id)}
    return {"success": True, "data": {k: _mask(v) for k, v in visible.items()}}


@router.post("")
async def create_profile(payload: ProfileRequest, request: Request) -> dict:
    owner_id = None if current_role(request) == "admin" else current_user_id(request)
    profile = Profile(**payload.model_dump(), owner_id=owner_id)
    _validate_stt_model(profile)
    profile_store.upsert(profile)
    return {"success": True, "data": _mask(profile)}


@router.get("/{name}")
async def get_profile(name: str, request: Request) -> dict:
    profile = profile_store.get(name)
    if not profile or not _visible(profile, current_user_id(request)):
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    return {"success": True, "data": _mask(profile)}


@router.put("/{name}")
async def update_profile(name: str, payload: ProfileRequest, request: Request) -> dict:
    existing = profile_store.get(name)
    if not existing or not _visible(existing, current_user_id(request)):
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    data = payload.model_dump()
    data["name"] = name
    data["owner_id"] = existing.owner_id
    if not data.get("llm", {}).get("api_key"):
        if existing.llm.api_key:
            data.setdefault("llm", {})["api_key"] = existing.llm.api_key
    profile = Profile(**data)
    _validate_stt_model(profile)
    profile_store.upsert(profile)
    return {"success": True, "data": _mask(profile)}


@router.delete("/{name}")
async def delete_profile(name: str, request: Request) -> dict:
    existing = profile_store.get(name)
    if not existing or not _visible(existing, current_user_id(request)):
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    profile_store.delete(name)
    return {"success": True, "data": {"name": name, "deleted": True}}


@router.post("/{name}/clone")
async def clone_profile(name: str, payload: CloneRequest, request: Request) -> dict:
    user_id = current_user_id(request)
    source = profile_store.get(name)
    if not source or not _visible(source, user_id):
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    existing_visible = {
        k for k, v in profile_store.list().items() if _visible(v, user_id)
    }
    if payload.new_name in existing_visible:
        raise HTTPException(status_code=409, detail=f"'{payload.new_name}' already exists")
    data = source.model_dump()
    data["name"] = payload.new_name
    data["owner_id"] = user_id
    clone = Profile(**data)
    profile_store.upsert(clone)
    return {"success": True, "data": _mask(clone)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_actor.py tests/unit/test_profile_ownership.py -v`
Expected: 11 passed (4 from Step 0 + 7 from Step 1)

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/core/actor.py tests/unit/test_actor.py apps/api_gateway/app/services/profiles/models.py apps/api_gateway/app/api/routes/profiles.py tests/unit/test_profile_ownership.py
git commit -m "feat(ownership): scope Profile to owner_id, add clone-from-template"
```

---

## Task 2: `TtsProfile` + `McpServer` ownership (owner_id, scoped routes, clone)

**Files:**
- Modify: `apps/api_gateway/app/services/tts/profile_models.py`
- Modify: `apps/api_gateway/app/api/routes/tts_profiles.py`
- Modify: `apps/api_gateway/app/services/mcp/models.py`
- Modify: `apps/api_gateway/app/api/routes/mcp.py`
- Test: `tests/unit/test_tts_profile_ownership.py`
- Test: `tests/unit/test_mcp_ownership.py`

**Interfaces:**
- Consumes: same session/role pattern as Task 1.
- Produces: `TtsProfile.owner_id`/`McpServer.owner_id`; same list/create/get/update/delete/clone scoping shape as Task 1, applied to `apps/api_gateway/app/api/routes/tts_profiles.py` (prefix `/v1/tts/profiles`) and `apps/api_gateway/app/api/routes/mcp.py` (prefix `/v1/mcp`, nested under `/servers`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_tts_profile_ownership.py
import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _signup_login(client, username: str, role: str = "user") -> None:
    client.post("/api/auth/signup", json={"username": username, "password": "pw"})
    if role == "admin":
        import asyncio

        from app.services.auth.users import user_store

        user = asyncio.run(user_store.get_by_username(username))
        asyncio.run(user_store.set_fields(user.id, role="admin"))
    client.post("/api/auth/login", json={"username": username, "password": "pw"})


def test_admin_created_tts_profile_is_template(client, _with_password):
    _signup_login(client, "root", role="admin")
    resp = client.post("/v1/tts/profiles", json={"name": "shared-voice"})
    assert resp.json()["data"]["owner_id"] is None


def test_user_created_tts_profile_is_owned_and_others_hidden(client, _with_password):
    _signup_login(client, "a", role="user")
    client.post("/v1/tts/profiles", json={"name": "a-voice"})

    _signup_login(client, "b", role="user")
    resp = client.get("/v1/tts/profiles/a-voice")
    assert resp.status_code == 404
    assert "a-voice" not in client.get("/v1/tts/profiles").json()["data"]


def test_clone_tts_profile(client, _with_password):
    _signup_login(client, "root", role="admin")
    client.post("/v1/tts/profiles", json={"name": "template-voice"})

    _signup_login(client, "toan", role="user")
    resp = client.post("/v1/tts/profiles/template-voice/clone", json={"new_name": "toan-voice"})
    assert resp.status_code == 200
    assert resp.json()["data"]["owner_id"] is not None


def test_create_rejects_name_taken_by_another_users_private_profile(client, _with_password):
    _signup_login(client, "a", role="user")
    client.post("/v1/tts/profiles", json={"name": "a-secret-voice"})

    _signup_login(client, "b", role="user")
    resp = client.post("/v1/tts/profiles", json={"name": "a-secret-voice"})
    assert resp.status_code == 409
    # confirm a's row survived untouched
    _signup_login(client, "a", role="user")
    assert client.get("/v1/tts/profiles/a-secret-voice").status_code == 200


def test_regular_user_cannot_update_or_delete_admin_template(client, _with_password):
    _signup_login(client, "root", role="admin")
    client.post("/v1/tts/profiles", json={"name": "template-voice-2"})

    _signup_login(client, "mallory", role="user")
    resp = client.put("/v1/tts/profiles/template-voice-2", json={"name": "template-voice-2"})
    assert resp.status_code == 404
    resp = client.delete("/v1/tts/profiles/template-voice-2")
    assert resp.status_code == 404
    assert client.get("/v1/tts/profiles/template-voice-2").status_code == 200
```

```python
# tests/unit/test_mcp_ownership.py
import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _signup_login(client, username: str, role: str = "user") -> None:
    client.post("/api/auth/signup", json={"username": username, "password": "pw"})
    if role == "admin":
        import asyncio

        from app.services.auth.users import user_store

        user = asyncio.run(user_store.get_by_username(username))
        asyncio.run(user_store.set_fields(user.id, role="admin"))
    client.post("/api/auth/login", json={"username": username, "password": "pw"})


def test_user_created_mcp_server_hidden_from_other_users(client, _with_password):
    _signup_login(client, "a", role="user")
    client.post(
        "/v1/mcp/servers",
        json={"name": "a-secret-server", "url": "https://a.example.com/mcp", "headers": {"X-Api-Key": "s3cr3t"}},
    )

    _signup_login(client, "b", role="user")
    assert "a-secret-server" not in client.get("/v1/mcp/servers").json()["data"]
    resp = client.get("/v1/mcp/servers/a-secret-server")
    assert resp.status_code == 404


def test_create_rejects_name_taken_by_another_users_private_server(client, _with_password):
    _signup_login(client, "a", role="user")
    client.post("/v1/mcp/servers", json={"name": "a-secret-server", "url": "https://a.example.com/mcp"})

    _signup_login(client, "b", role="user")
    resp = client.post("/v1/mcp/servers", json={"name": "a-secret-server", "url": "https://b.example.com/mcp"})
    assert resp.status_code == 409
    # confirm a's row (and its own url) survived untouched
    _signup_login(client, "a", role="user")
    got = client.get("/v1/mcp/servers/a-secret-server")
    assert got.status_code == 200
    assert got.json()["data"]["url"] == "https://a.example.com/mcp"


def test_regular_user_cannot_update_or_delete_admin_template(client, _with_password):
    _signup_login(client, "root", role="admin")
    client.post("/v1/mcp/servers", json={"name": "template-mcp-2", "url": "https://t.example.com/mcp"})

    _signup_login(client, "mallory", role="user")
    resp = client.put(
        "/v1/mcp/servers/template-mcp-2",
        json={"name": "template-mcp-2", "url": "https://mallory.example.com/mcp"},
    )
    assert resp.status_code == 404
    resp = client.delete("/v1/mcp/servers/template-mcp-2")
    assert resp.status_code == 404
    got = client.get("/v1/mcp/servers/template-mcp-2")
    assert got.status_code == 200
    assert got.json()["data"]["url"] == "https://t.example.com/mcp"


def test_clone_mcp_server(client, _with_password):
    _signup_login(client, "root", role="admin")
    client.post("/v1/mcp/servers", json={"name": "template-mcp", "url": "https://t.example.com/mcp"})

    _signup_login(client, "toan", role="user")
    resp = client.post("/v1/mcp/servers/template-mcp/clone", json={"new_name": "toan-mcp"})
    assert resp.status_code == 200
    assert resp.json()["data"]["owner_id"] is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_tts_profile_ownership.py tests/unit/test_mcp_ownership.py -v`
Expected: FAIL — no `owner_id`, no scoping, no clone route

- [ ] **Step 3: Write the implementation**

In `apps/api_gateway/app/services/tts/profile_models.py`, add to `TtsProfile` (after `name`):

```python
    owner_id: str | None = None
```

Replace `apps/api_gateway/app/api/routes/tts_profiles.py`. Two authorization predicates,
matching the pattern Task 1 established (and had to fix after its first review found
two Critical bugs — repeated here from the start): `_visible` gates reads (a template
is visible to everyone), `_can_write` gates writes (a template may only be written by
an admin; an owned row only by its owner) — and `create`/`clone` check *existence*
(`tts_profile_store.get(...) is not None`), not visibility, before writing, so a name
already taken by another user's invisible private row can't be silently overwritten:

```python
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core.actor import current_role, current_user_id
from app.services.tts.profile_models import TtsProfile
from app.services.tts.profile_store import tts_profile_store

router = APIRouter(prefix="/v1/tts/profiles", tags=["tts"])


def _visible(profile: TtsProfile, user_id: str | None) -> bool:
    return profile.owner_id is None or profile.owner_id == user_id


def _can_write(profile: TtsProfile, user_id: str | None, role: str) -> bool:
    if profile.owner_id is None:
        return role == "admin"
    return profile.owner_id == user_id


class CloneRequest(BaseModel):
    new_name: str


@router.get("")
async def list_tts_profiles(request: Request) -> dict:
    user_id = current_user_id(request)
    profiles = tts_profile_store.list()
    visible = {k: v for k, v in profiles.items() if _visible(v, user_id)}
    return {"success": True, "data": {k: v.model_dump() for k, v in visible.items()}}


@router.post("")
async def create_tts_profile(payload: TtsProfile, request: Request) -> dict:
    if tts_profile_store.get(payload.name) is not None:
        raise HTTPException(status_code=409, detail=f"'{payload.name}' already exists")
    owner_id = None if current_role(request) == "admin" else current_user_id(request)
    profile = payload.model_copy(update={"owner_id": owner_id})
    tts_profile_store.upsert(profile)
    return {"success": True, "data": profile.model_dump()}


@router.get("/{name}")
async def get_tts_profile(name: str, request: Request) -> dict:
    profile = tts_profile_store.get(name)
    if not profile or not _visible(profile, current_user_id(request)):
        raise HTTPException(status_code=404, detail=f"TTS profile '{name}' not found")
    return {"success": True, "data": profile.model_dump()}


@router.put("/{name}")
async def update_tts_profile(name: str, payload: TtsProfile, request: Request) -> dict:
    existing = tts_profile_store.get(name)
    if not existing or not _can_write(existing, current_user_id(request), current_role(request)):
        raise HTTPException(status_code=404, detail=f"TTS profile '{name}' not found")
    data = payload.model_dump()
    data["name"] = name
    data["owner_id"] = existing.owner_id
    profile = TtsProfile(**data)
    tts_profile_store.upsert(profile)
    return {"success": True, "data": profile.model_dump()}


@router.delete("/{name}")
async def delete_tts_profile(name: str, request: Request) -> dict:
    existing = tts_profile_store.get(name)
    if not existing or not _can_write(existing, current_user_id(request), current_role(request)):
        raise HTTPException(status_code=404, detail=f"TTS profile '{name}' not found")
    tts_profile_store.delete(name)
    return {"success": True, "data": {"name": name, "deleted": True}}


@router.post("/{name}/clone")
async def clone_tts_profile(name: str, payload: CloneRequest, request: Request) -> dict:
    user_id = current_user_id(request)
    source = tts_profile_store.get(name)
    if not source or not _visible(source, user_id):
        raise HTTPException(status_code=404, detail=f"TTS profile '{name}' not found")
    if tts_profile_store.get(payload.new_name) is not None:
        raise HTTPException(status_code=409, detail=f"'{payload.new_name}' already exists")
    data = source.model_dump()
    data["name"] = payload.new_name
    data["owner_id"] = user_id
    clone = TtsProfile(**data)
    tts_profile_store.upsert(clone)
    return {"success": True, "data": clone.model_dump()}
```

In `apps/api_gateway/app/services/mcp/models.py`, add to `McpServer` (after `name`):

```python
    owner_id: str | None = None
```

Replace `apps/api_gateway/app/api/routes/mcp.py`. Same `_visible`/`_can_write` split and
existence-check-before-write as `tts_profiles.py` above:

```python
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core.actor import current_role, current_user_id
from app.services.mcp.models import McpServer
from app.services.mcp.pool import mcp_pool
from app.services.mcp.presets import PRESET_NAMES
from app.services.mcp.server_store import mcp_server_store

router = APIRouter(prefix="/v1/mcp", tags=["mcp"])


def _visible(server: McpServer, user_id: str | None) -> bool:
    return server.owner_id is None or server.owner_id == user_id


def _can_write(server: McpServer, user_id: str | None, role: str) -> bool:
    if server.owner_id is None:
        return role == "admin"
    return server.owner_id == user_id


class McpServerRequest(BaseModel):
    name: str
    url: str
    headers: dict[str, str] = {}
    enabled: bool = True


class McpServerEnabledRequest(BaseModel):
    enabled: bool


class CloneRequest(BaseModel):
    new_name: str


@router.get("/servers")
async def list_servers(request: Request) -> dict:
    user_id = current_user_id(request)
    servers = mcp_server_store.list()
    visible = {k: v for k, v in servers.items() if _visible(v, user_id)}
    return {"success": True, "data": {k: v.model_dump() for k, v in visible.items()}}


@router.post("/servers")
async def add_server(payload: McpServerRequest, request: Request) -> dict:
    if mcp_server_store.get(payload.name) is not None:
        raise HTTPException(status_code=409, detail=f"'{payload.name}' already exists")
    owner_id = None if current_role(request) == "admin" else current_user_id(request)
    entry = McpServer(
        name=payload.name, url=payload.url, headers=payload.headers,
        enabled=payload.enabled, owner_id=owner_id,
    )
    mcp_server_store.upsert(entry)
    return {"success": True, "data": entry.model_dump()}


@router.get("/servers/{name}")
async def get_server(name: str, request: Request) -> dict:
    entry = mcp_server_store.get(name)
    if not entry or not _visible(entry, current_user_id(request)):
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    return {"success": True, "data": entry.model_dump()}


@router.put("/servers/{name}")
async def update_server(name: str, payload: McpServerRequest, request: Request) -> dict:
    old = mcp_server_store.get(name)
    if not old or not _can_write(old, current_user_id(request), current_role(request)):
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    mcp_pool.invalidate(old.url)
    entry = McpServer(
        name=name, url=payload.url, headers=payload.headers,
        enabled=payload.enabled, owner_id=old.owner_id,
    )
    mcp_server_store.upsert(entry)
    return {"success": True, "data": entry.model_dump()}


@router.patch("/servers/{name}/enabled")
async def set_server_enabled(name: str, payload: McpServerEnabledRequest, request: Request) -> dict:
    entry = mcp_server_store.get(name)
    if not entry or not _can_write(entry, current_user_id(request), current_role(request)):
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    updated = entry.model_copy(update={"enabled": payload.enabled})
    mcp_server_store.upsert(updated)
    return {"success": True, "data": updated.model_dump()}


@router.delete("/servers/{name}")
async def delete_server(name: str, request: Request) -> dict:
    if name in PRESET_NAMES:
        raise HTTPException(
            status_code=400,
            detail=f"'{name}' is a built-in preset; disable it instead of deleting it",
        )
    entry = mcp_server_store.get(name)
    if not entry or not _can_write(entry, current_user_id(request), current_role(request)):
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    mcp_pool.invalidate(entry.url)
    mcp_server_store.delete(name)
    return {"success": True, "data": {"name": name, "deleted": True}}


@router.get("/servers/{name}/tools")
async def list_server_tools(name: str, request: Request) -> dict:
    entry = mcp_server_store.get(name)
    if not entry or not _visible(entry, current_user_id(request)):
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    mcp_pool.invalidate(entry.url)
    tools = await mcp_pool.get_tools(entry.url, headers=entry.headers)
    return {"success": True, "data": {"server": name, "url": entry.url, "tools": tools}}


@router.post("/servers/{name}/clone")
async def clone_server(name: str, payload: CloneRequest, request: Request) -> dict:
    user_id = current_user_id(request)
    source = mcp_server_store.get(name)
    if not source or not _visible(source, user_id):
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    if mcp_server_store.get(payload.new_name) is not None:
        raise HTTPException(status_code=409, detail=f"'{payload.new_name}' already exists")
    clone = McpServer(
        name=payload.new_name, url=source.url, headers=source.headers,
        enabled=source.enabled, owner_id=user_id,
    )
    mcp_server_store.upsert(clone)
    return {"success": True, "data": clone.model_dump()}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_tts_profile_ownership.py tests/unit/test_mcp_ownership.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/tts/profile_models.py apps/api_gateway/app/api/routes/tts_profiles.py apps/api_gateway/app/services/mcp/models.py apps/api_gateway/app/api/routes/mcp.py tests/unit/test_tts_profile_ownership.py tests/unit/test_mcp_ownership.py
git commit -m "feat(ownership): scope TtsProfile/McpServer to owner_id, add clone-from-template"
```

---

## Task 3: `ChatSession` ownership (migration helper, session_store, sessions routes)

**Files:**
- Modify: `apps/api_gateway/app/services/db/models.py`
- Modify: `apps/api_gateway/app/services/db/engine.py`
- Modify: `apps/api_gateway/app/services/history/store.py`
- Modify: `apps/api_gateway/app/services/conversation/session.py`
- Modify: `apps/api_gateway/app/api/routes/conversation.py`
- Modify: `apps/api_gateway/app/api/routes/livehost.py`
- Modify: `apps/api_gateway/app/api/routes/sessions.py`
- Test: `tests/unit/test_ensure_column.py`
- Test: `tests/unit/test_session_store.py` (extend — this file already exists)
- Test: `tests/unit/test_sessions_routes.py` (extend — this file already exists)

**Interfaces:**
- Produces: `ChatSession.user_id: str | None`; `_ensure_column(conn, table, column, ddl_type)` (async helper in `db/engine.py`); `SessionStore.create(session_id, profile_id="", meta=None, user_id=None)`; `SessionStore.list(profile_id=None, user_id=None, limit=20, offset=0)`; `GET/DELETE /v1/sessions*` scoped to the caller unless admin.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_ensure_column.py
import pytest
from sqlalchemy import text

from app.services.db.engine import _ensure_column, db_session


@pytest.mark.asyncio
async def test_ensure_column_adds_missing_column_once():
    async with db_session() as s:
        await s.execute(text("CREATE TABLE IF NOT EXISTS _ensure_column_test (id VARCHAR(36) PRIMARY KEY)"))
        await s.commit()
        conn = await s.connection()
        await _ensure_column(conn, "_ensure_column_test", "extra", "VARCHAR(36)")
        await _ensure_column(conn, "_ensure_column_test", "extra", "VARCHAR(36)")  # idempotent, no error
        result = await s.execute(text("PRAGMA table_info(_ensure_column_test)"))
        columns = {row[1] for row in result.fetchall()}
        assert "extra" in columns
```

Add to `tests/unit/test_session_store.py` (existing file):

```python
@pytest.mark.asyncio
async def test_create_with_user_id_and_filter(store):
    await store.create("s1", profile_id="pet", user_id="user-a")
    await store.create("s2", profile_id="pet", user_id="user-b")
    rows = await store.list(user_id="user-a")
    assert [r["id"] for r in rows] == ["s1"]
    got = await store.get("s1")
    assert got["user_id"] == "user-a"
```

The existing `tests/unit/test_sessions_routes.py` has no login/signup helper at all (its 10 pre-existing tests call the routes with no session, relying on `settings.admin_password` being unset by default) — this is exactly the dev-mode/unfiltered path `_scope_user_id`'s fallback must preserve, so leave those 10 tests untouched. Append a new, fully self-contained test with its own fixtures:

```python
@pytest.fixture
def _with_password(monkeypatch):
    from app.core.settings import settings

    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _signup_login(client, username: str, role: str = "user") -> None:
    client.post("/api/auth/signup", json={"username": username, "password": "pw"})
    if role == "admin":
        import asyncio

        from app.services.auth.users import user_store

        user = asyncio.run(user_store.get_by_username(username))
        asyncio.run(user_store.set_fields(user.id, role="admin"))
    client.post("/api/auth/login", json={"username": username, "password": "pw"})


def test_sessions_scoped_to_owner_unless_admin(client, _with_password):
    _signup_login(client, "a", role="user")
    import asyncio

    from app.services.auth.users import user_store
    from app.services.history.store import session_store

    user_a = asyncio.run(user_store.get_by_username("a"))
    asyncio.run(session_store.create("sess-a", profile_id="p", user_id=user_a.id))
    asyncio.run(session_store.create("sess-orphan", profile_id="p", user_id=None))

    _signup_login(client, "a", role="user")
    rows = client.get("/v1/sessions").json()["data"]
    assert {r["id"] for r in rows} == {"sess-a"}

    _signup_login(client, "root", role="admin")
    rows = client.get("/v1/sessions").json()["data"]
    assert {"sess-a", "sess-orphan"}.issubset({r["id"] for r in rows})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_ensure_column.py tests/unit/test_session_store.py tests/unit/test_sessions_routes.py -v`
Expected: FAIL — `_ensure_column` doesn't exist; `create()`/`list()` don't accept `user_id`

- [ ] **Step 3: Write the implementation**

In `apps/api_gateway/app/services/db/models.py`, add to `ChatSession` (after `profile_id`):

```python
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
```

In `apps/api_gateway/app/services/db/engine.py`, add the helper and call it from `init_db()`:

```python
async def _ensure_column(conn, table: str, column: str, ddl_type: str) -> None:
    """Idempotent ALTER TABLE ADD COLUMN -- this codebase has no migration
    framework, and Base.metadata.create_all only creates missing tables, never
    alters existing ones. Safe to call every startup."""
    result = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
    existing = {row[1] for row in result.fetchall()}
    if column not in existing:
        await conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
```

Update `init_db()`:

```python
async def init_db() -> None:
    """Create tables once (idempotent, concurrency-safe)."""
    from app.services.db.models import Base

    global _initialized
    if _factory is None:
        configure()
    async with _init_lock:
        if _initialized:
            return
        assert _engine is not None
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await _ensure_column(conn, "sessions", "user_id", "VARCHAR(36)")
        _initialized = True
```

In `apps/api_gateway/app/services/history/store.py`, update `_session_dict`, `create`, and `list`:

```python
def _session_dict(s: ChatSession) -> dict:
    return {
        "id": s.id,
        "profile_id": s.profile_id,
        "user_id": s.user_id,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "ended_at": s.ended_at.isoformat() if s.ended_at else None,
        "meta": s.meta or {},
    }


class SessionStore:
    async def create(
        self, session_id: str, profile_id: str = "", meta: dict | None = None,
        user_id: str | None = None,
    ) -> dict:
        async with db_session() as s:
            row = ChatSession(id=session_id, profile_id=profile_id, meta=meta or {}, user_id=user_id)
            s.add(row)
            await s.commit()
            return _session_dict(row)
```

(leave `get`/`exists`/`append_message`/`mark_ended`/`delete`/`delete_many` unchanged)

```python
    async def list(
        self, profile_id: str | None = None, user_id: str | None = None,
        limit: int = 20, offset: int = 0,
    ) -> list[dict]:
        async with db_session() as s:
            q = select(ChatSession).order_by(ChatSession.created_at.desc())
            if profile_id is not None:
                q = q.where(ChatSession.profile_id == profile_id)
            if user_id is not None:
                q = q.where(ChatSession.user_id == user_id)
            rows = (await s.execute(q.limit(limit).offset(offset))).scalars().all()
            out = []
            for row in rows:
                count = (
                    await s.execute(
                        select(func.count(ChatMessage.id)).where(ChatMessage.session_id == row.id)
                    )
                ).scalar_one()
                first = (
                    await s.execute(
                        select(ChatMessage.content)
                        .where(ChatMessage.session_id == row.id, ChatMessage.role == "user")
                        .order_by(ChatMessage.id)
                        .limit(1)
                    )
                ).scalar_one_or_none()
                d = _session_dict(row)
                d["message_count"] = count
                d["preview"] = (first or "")[:80]
                out.append(d)
            return out
```

(`clear(profile_id=None, only_empty=False)` stays profile-only — `DELETE /v1/sessions` without a `user_id` filter is only reachable by admins per the route change below, so this method doesn't need a new param.)

In `apps/api_gateway/app/services/conversation/session.py`, change the `session_store.create(...)` call inside `start()` (the `else` branch after the `resume_sid` check):

```python
            else:
                await session_store.create(
                    cfg.session_id,
                    profile_id=cfg.profile_name or "",
                    meta={"stt_engine": cfg.stt_engine, "tts_engine": cfg.tts_engine},
                    user_id=profile.owner_id if profile else None,
                )
```

In `apps/api_gateway/app/api/routes/conversation.py`, change the REST chat handler's create call:

```python
            await session_store.create(sid, profile_id=profile or "", user_id=active_profile.owner_id if active_profile else None)
```

In `apps/api_gateway/app/api/routes/livehost.py`, change the WS handler's create call:

```python
        await session_store.create(
            session_id, profile_id=profile_name or "",
            meta={"stt_engine": stt_engine, "tts_engine": tts_engine, "livehost": True},
            user_id=profile.owner_id if profile else None,
        )
```

Replace `apps/api_gateway/app/api/routes/sessions.py`:

```python
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core.actor import current_role, current_user_id
from app.services.history.store import session_store

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])


class BulkDeleteRequest(BaseModel):
    ids: list[str] = []


def _scope_user_id(request: Request) -> str | None:
    """None for admins, or when auth is fully disabled (dev mode -- see
    app.core.actor.current_role), which is unfiltered/unchanged from today's
    behavior; the caller's own id otherwise, so a regular user only ever sees
    their own sessions."""
    return None if current_role(request) == "admin" else current_user_id(request)


@router.get("")
async def list_sessions(request: Request, profile: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    rows = await session_store.list(
        profile_id=profile, user_id=_scope_user_id(request), limit=limit, offset=offset
    )
    return {"success": True, "data": rows}


@router.post("/bulk_delete")
async def bulk_delete_sessions(payload: BulkDeleteRequest, request: Request) -> dict:
    """Delete the listed sessions. Missing IDs are skipped, not errors. A
    non-admin's request is first filtered down to only the ids they own."""
    ids = payload.ids
    scope = _scope_user_id(request)
    if scope is not None:
        owned = {r["id"] for r in await session_store.list(user_id=scope, limit=10_000)}
        ids = [i for i in ids if i in owned]
    deleted = await session_store.delete_many(ids)
    return {"success": True, "data": {"deleted": deleted}}


@router.delete("")
async def clear_sessions(request: Request, profile: str | None = None, only_empty: bool = False) -> dict:
    """Clear sessions in scope. Non-admins may only clear their own; enforced by
    deleting via bulk_delete-style id filtering rather than the unfiltered
    profile-only clear() method, which stays admin-only."""
    scope = _scope_user_id(request)
    if scope is None:
        deleted = await session_store.clear(profile_id=profile, only_empty=only_empty)
        return {"success": True, "data": {"deleted": deleted}}
    owned = await session_store.list(profile_id=profile, user_id=scope, limit=10_000)
    ids = [r["id"] for r in owned if not only_empty or r["message_count"] == 0]
    deleted = await session_store.delete_many(ids)
    return {"success": True, "data": {"deleted": deleted}}


@router.get("/{session_id}")
async def get_session(session_id: str, request: Request) -> dict:
    sess = await session_store.get(session_id)
    scope = _scope_user_id(request)
    if not sess or (scope is not None and sess.get("user_id") != scope):
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    sess["messages"] = await session_store.get_messages(session_id)
    return {"success": True, "data": sess}


@router.delete("/{session_id}")
async def delete_session(session_id: str, request: Request) -> dict:
    sess = await session_store.get(session_id)
    scope = _scope_user_id(request)
    if not sess or (scope is not None and sess.get("user_id") != scope):
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    await session_store.delete(session_id)
    return {"success": True, "data": {"id": session_id, "deleted": True}}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_ensure_column.py tests/unit/test_session_store.py tests/unit/test_sessions_routes.py tests/integration/test_conversation_history.py -v`
Expected: all pass (including pre-existing tests in the extended files, confirming `user_id` is optional/backward compatible)

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/db/models.py apps/api_gateway/app/services/db/engine.py apps/api_gateway/app/services/history/store.py apps/api_gateway/app/services/conversation/session.py apps/api_gateway/app/api/routes/conversation.py apps/api_gateway/app/api/routes/livehost.py apps/api_gateway/app/api/routes/sessions.py tests/unit/test_ensure_column.py tests/unit/test_session_store.py tests/unit/test_sessions_routes.py
git commit -m "feat(ownership): scope ChatSession to user_id via profile ownership"
```

---

## Task 4: Memory ownership (`MemoryItem`/`MemoryProfileDoc` user_id)

**Files:**
- Modify: `apps/api_gateway/app/services/db/models.py`
- Modify: `apps/api_gateway/app/services/db/engine.py`
- Modify: `apps/api_gateway/app/services/memory/store.py`
- Modify: `apps/api_gateway/app/services/memory/extractor.py`
- Test: `tests/unit/test_memory_store.py` (extend if it exists, else create)

**Interfaces:**
- Consumes: `_ensure_column` (Task 3).
- Produces: `MemoryItem.user_id`/`MemoryProfileDoc.user_id`; `MemoryStore.add(profile_id, content, source_session_id=None, embedding=None, user_id=None)`; `memory_extractor.extract_and_upsert` passes `profile.owner_id` through to `memory_store.add` (no signature change to `extract_and_upsert` itself — it already receives `profile`).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_memory_store.py
import pytest

from app.services.memory.store import MemoryStore


@pytest.fixture
def store():
    return MemoryStore()


@pytest.mark.asyncio
async def test_add_with_user_id_roundtrips(store):
    added = await store.add("profile-a", "likes tea", user_id="user-a")
    assert added["user_id"] == "user-a"
    items = await store.list("profile-a")
    assert items[0]["user_id"] == "user-a"


@pytest.mark.asyncio
async def test_add_without_user_id_defaults_none(store):
    added = await store.add("profile-a", "likes coffee")
    assert added["user_id"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_memory_store.py -v`
Expected: FAIL — `add()` doesn't accept `user_id`, `_mem_dict` doesn't include it

- [ ] **Step 3: Write the implementation**

In `apps/api_gateway/app/services/db/models.py`, add to `MemoryItem` and `MemoryProfileDoc` (after `profile_id` in each):

```python
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
```

In `apps/api_gateway/app/services/db/engine.py`'s `init_db()`, add two more `_ensure_column` calls alongside the `sessions` one from Task 3:

```python
            await _ensure_column(conn, "memories", "user_id", "VARCHAR(36)")
            await _ensure_column(conn, "memory_profile_docs", "user_id", "VARCHAR(36)")
```

In `apps/api_gateway/app/services/memory/store.py`, update `_mem_dict` and `add`:

```python
def _mem_dict(m: MemoryItem) -> dict:
    return {
        "id": m.id,
        "profile_id": m.profile_id,
        "user_id": m.user_id,
        "content": m.content,
        "source_session_id": m.source_session_id,
        "embedding": m.embedding,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }
```

```python
    async def add(
        self,
        profile_id: str,
        content: str,
        source_session_id: str | None = None,
        embedding: list[float] | None = None,
        user_id: str | None = None,
    ) -> dict:
        async with db_session() as s:
            row = MemoryItem(
                id=str(uuid.uuid4()),
                profile_id=profile_id,
                content=content,
                source_session_id=source_session_id,
                embedding=embedding,
                user_id=user_id,
            )
            s.add(row)
            await s.commit()
            return _mem_dict(row)
```

In `apps/api_gateway/app/services/memory/extractor.py`, update the one call site inside `extract_and_upsert` (the `await memory_store.add(...)` call in the `for fact, vec in zip(facts, new_vecs):` loop):

```python
                await memory_store.add(
                    profile.name, fact, source_session_id=session_id, embedding=vec,
                    user_id=profile.owner_id,
                )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_memory_store.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/db/models.py apps/api_gateway/app/services/db/engine.py apps/api_gateway/app/services/memory/store.py apps/api_gateway/app/services/memory/extractor.py tests/unit/test_memory_store.py
git commit -m "feat(ownership): scope MemoryItem/MemoryProfileDoc to user_id via profile ownership"
```

---

## Task 5: `model_registry_entries` table + seeding + `check_model_allowed` gate

**Files:**
- Modify: `apps/api_gateway/app/services/db/models.py`
- Modify: `apps/api_gateway/app/core/errors.py`
- Create: `apps/api_gateway/app/services/model_registry/__init__.py` (empty)
- Create: `apps/api_gateway/app/services/model_registry/store.py`
- Create: `apps/api_gateway/app/services/model_registry/gate.py`
- Create: `apps/api_gateway/app/services/model_registry/seed.py`
- Modify: `apps/api_gateway/app/main.py`
- Test: `tests/unit/test_model_registry_store.py`
- Test: `tests/unit/test_model_registry_gate.py`
- Test: `tests/unit/test_model_registry_seed.py`

**Interfaces:**
- Consumes: `STT_MODEL_REGISTRIES` (`app.services.stt.model_registry`, existing); `tts_service.providers` (`app.services.tts.service`, existing); `User.can_use_testing` (existing).
- Produces: `ModelRegistryEntry` SQLAlchemy model; `ModelRegistryStore` (`app.services.model_registry.store`, singleton `model_registry_store`) with `async list_all() -> list[dict]`, `async find(kind, engine, model_id) -> ModelRegistryEntry | None`, `async create(kind, engine, model_id, label, stage="stable") -> dict` (always `enabled=True`), `async set_fields(entry_id, **fields) -> dict | None`; `async def check_model_allowed(kind: str, engine: str, model_id: str, user) -> None` (`app.services.model_registry.gate`, raises `ModelNotAllowedError`); `async def seed_known_models() -> None` (`app.services.model_registry.seed`, idempotent — only inserts entries that don't already exist by `(kind, engine, model_id)`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_model_registry_store.py
import pytest

from app.services.model_registry.store import ModelRegistryStore


@pytest.fixture
def store():
    return ModelRegistryStore()


@pytest.mark.asyncio
async def test_create_defaults_enabled_true_stable(store):
    entry = await store.create("stt", "whisper", "medium", "Whisper Medium")
    assert entry["enabled"] is True
    assert entry["stage"] == "stable"


@pytest.mark.asyncio
async def test_find_matches_exact_triple(store):
    await store.create("stt", "whisper", "medium", "Whisper Medium")
    found = await store.find("stt", "whisper", "medium")
    assert found is not None
    assert await store.find("stt", "whisper", "large") is None


@pytest.mark.asyncio
async def test_set_fields_updates_enabled_and_stage(store):
    created = await store.create("llm", "openrouter", "qwen3-asr-flash", "Qwen3 ASR Flash (OpenRouter)")
    updated = await store.set_fields(created["id"], enabled=False, stage="testing")
    assert updated["enabled"] is False
    assert updated["stage"] == "testing"
    assert await store.set_fields("missing-id", enabled=False) is None


@pytest.mark.asyncio
async def test_list_all_returns_every_entry(store):
    await store.create("stt", "whisper", "medium", "Whisper Medium")
    await store.create("tts", "omnivoice", "omnivoice", "OmniVoice")
    entries = await store.list_all()
    assert len(entries) == 2
```

```python
# tests/unit/test_model_registry_gate.py
import pytest

from app.core.errors import ModelNotAllowedError
from app.services.auth.users import UserStore
from app.services.model_registry.gate import check_model_allowed
from app.services.model_registry.store import ModelRegistryStore


@pytest.fixture
def store():
    return ModelRegistryStore()


@pytest.fixture
def users():
    return UserStore()


@pytest.mark.asyncio
async def test_no_matching_entry_is_unrestricted(store, users):
    user = await users.create("toan", "pw")
    await check_model_allowed("llm", "some-custom-engine", "some-model", user)  # no raise


@pytest.mark.asyncio
async def test_disabled_entry_is_rejected(store, users):
    user = await users.create("toan", "pw")
    created = await store.create("stt", "whisper", "medium", "Whisper Medium")
    await store.set_fields(created["id"], enabled=False)
    with pytest.raises(ModelNotAllowedError):
        await check_model_allowed("stt", "whisper", "medium", user)


@pytest.mark.asyncio
async def test_testing_stage_requires_can_use_testing(store, users):
    regular = await users.create("toan", "pw")
    tester = await users.create("linh", "pw")
    await users.set_fields(tester["id"], can_use_testing=True)
    created = await store.create("llm", "openrouter", "qwen3-asr-flash", "Qwen3 ASR Flash", stage="testing")

    regular_user = await users.get_by_id(regular["id"])
    tester_user = await users.get_by_id(tester["id"])

    with pytest.raises(ModelNotAllowedError):
        await check_model_allowed("llm", "openrouter", "qwen3-asr-flash", regular_user)
    await check_model_allowed("llm", "openrouter", "qwen3-asr-flash", tester_user)  # no raise
```

```python
# tests/unit/test_model_registry_seed.py
import pytest

from app.services.model_registry.seed import seed_known_models
from app.services.model_registry.store import ModelRegistryStore


@pytest.fixture
def store():
    return ModelRegistryStore()


@pytest.mark.asyncio
async def test_seed_populates_stt_and_tts_entries(store):
    await seed_known_models()
    entries = await store.list_all()
    kinds = {e["kind"] for e in entries}
    assert "stt" in kinds
    assert "tts" in kinds
    # tts entries gate at engine granularity: model_id == engine
    tts_entries = [e for e in entries if e["kind"] == "tts"]
    assert all(e["model_id"] == e["engine"] for e in tts_entries)


@pytest.mark.asyncio
async def test_seed_is_idempotent_and_preserves_admin_edits(store):
    await seed_known_models()
    entries = await store.list_all()
    stt_entry = next(e for e in entries if e["kind"] == "stt")
    await store.set_fields(stt_entry["id"], enabled=False)

    await seed_known_models()  # re-seed must not overwrite the admin's edit
    refreshed = await store.find("stt", stt_entry["engine"], stt_entry["model_id"])
    assert refreshed.enabled is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_model_registry_store.py tests/unit/test_model_registry_gate.py tests/unit/test_model_registry_seed.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.model_registry'`

- [ ] **Step 3: Write the implementation**

Add to `apps/api_gateway/app/services/db/models.py` (append at end):

```python
class ModelRegistryEntry(Base):
    __tablename__ = "model_registry_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(8), index=True)   # "stt" | "tts" | "llm"
    engine: Mapped[str] = mapped_column(String(64), index=True)
    model_id: Mapped[str] = mapped_column(String(128), index=True)
    label: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    stage: Mapped[str] = mapped_column(String(16), default="stable")  # "stable" | "testing"
```

Add to `apps/api_gateway/app/core/errors.py` (after `DeviceSerialConflictError`):

```python
class ModelNotAllowedError(AppError):
    """Raised when a chosen (kind, engine, model_id) matches a registry entry
    that is disabled, or is stage=testing and the user lacks can_use_testing."""

    status_code = 403
```

```python
# apps/api_gateway/app/services/model_registry/__init__.py
```

```python
# apps/api_gateway/app/services/model_registry/store.py
from __future__ import annotations

import uuid

from sqlalchemy import select

from app.services.db.engine import db_session
from app.services.db.models import ModelRegistryEntry


def _entry_dict(e: ModelRegistryEntry) -> dict:
    return {
        "id": e.id, "kind": e.kind, "engine": e.engine, "model_id": e.model_id,
        "label": e.label, "enabled": e.enabled, "stage": e.stage,
    }


class ModelRegistryStore:
    async def list_all(self) -> list[dict]:
        async with db_session() as s:
            rows = (
                await s.execute(select(ModelRegistryEntry).order_by(
                    ModelRegistryEntry.kind, ModelRegistryEntry.engine, ModelRegistryEntry.model_id
                ))
            ).scalars().all()
            return [_entry_dict(e) for e in rows]

    async def find(self, kind: str, engine: str, model_id: str) -> ModelRegistryEntry | None:
        async with db_session() as s:
            return (
                await s.execute(
                    select(ModelRegistryEntry).where(
                        ModelRegistryEntry.kind == kind,
                        ModelRegistryEntry.engine == engine,
                        ModelRegistryEntry.model_id == model_id,
                    )
                )
            ).scalar_one_or_none()

    async def create(self, kind: str, engine: str, model_id: str, label: str, stage: str = "stable") -> dict:
        async with db_session() as s:
            row = ModelRegistryEntry(
                id=str(uuid.uuid4()), kind=kind, engine=engine, model_id=model_id,
                label=label, enabled=True, stage=stage,
            )
            s.add(row)
            await s.commit()
            return _entry_dict(row)

    async def set_fields(self, entry_id: str, **fields) -> dict | None:
        async with db_session() as s:
            row = await s.get(ModelRegistryEntry, entry_id)
            if row is None:
                return None
            for key, value in fields.items():
                setattr(row, key, value)
            await s.commit()
            return _entry_dict(row)


model_registry_store = ModelRegistryStore()
```

```python
# apps/api_gateway/app/services/model_registry/gate.py
"""Validation gate: a (kind, engine, model_id) choice is only restricted if an
admin has explicitly catalogued it in the model registry. No matching entry ->
unrestricted, preserving today's bring-your-own-endpoint flexibility for
anything not curated (e.g. a fully custom self-hosted LLM).

`user` may be None (route ran with no resolved acting user -- only possible
when settings.admin_password is unset, dev mode; see app.core.actor). The
`enabled` check still applies unconditionally; the `can_use_testing` check
fails closed (blocks) when there's no real user to check it against, since
that's the safer default for a permission question with no identity behind
it."""

from __future__ import annotations

from app.core.errors import ModelNotAllowedError
from app.services.db.models import User
from app.services.model_registry.store import model_registry_store


async def check_model_allowed(kind: str, engine: str, model_id: str, user: User | None) -> None:
    if not engine or not model_id:
        return
    entry = await model_registry_store.find(kind, engine, model_id)
    if entry is None:
        return
    if not entry.enabled:
        raise ModelNotAllowedError(f"{kind} model '{engine}/{model_id}' is currently disabled")
    if entry.stage == "testing" and not (user and user.can_use_testing):
        raise ModelNotAllowedError(
            f"{kind} model '{engine}/{model_id}' is in testing and not enabled for your account"
        )
```

```python
# apps/api_gateway/app/services/model_registry/seed.py
"""Idempotent startup seed: registers every model the STT registries and
installed TTS engines already know about, so an admin can toggle enabled/stage
on them without having to hand-enter every one first. Never overwrites an
existing entry (an admin's enabled/stage edit on a previously-seeded row must
survive a re-seed on the next boot)."""

from __future__ import annotations

from app.services.model_registry.store import model_registry_store
from app.services.stt.model_registry import STT_MODEL_REGISTRIES
from app.services.tts.service import tts_service


async def seed_known_models() -> None:
    for engine, registry in STT_MODEL_REGISTRIES.items():
        for m in registry.list_models():
            if await model_registry_store.find("stt", engine, m["id"]) is None:
                await model_registry_store.create("stt", engine, m["id"], m["label"])
    for engine_name in tts_service.providers:
        if await model_registry_store.find("tts", engine_name, engine_name) is None:
            await model_registry_store.create("tts", engine_name, engine_name, engine_name)
```

In `apps/api_gateway/app/main.py`'s `lifespan`, call the seed after the existing config-store seeding (`seed_default_servers(mcp_server_store)`):

```python
    from app.services.model_registry.seed import seed_known_models

    await seed_known_models()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_model_registry_store.py tests/unit/test_model_registry_gate.py tests/unit/test_model_registry_seed.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/db/models.py apps/api_gateway/app/core/errors.py apps/api_gateway/app/services/model_registry/ apps/api_gateway/app/main.py tests/unit/test_model_registry_store.py tests/unit/test_model_registry_gate.py tests/unit/test_model_registry_seed.py
git commit -m "feat(model-registry): add model_registry_entries table, gate, and startup seeding"
```

---

## Task 6: Wire `check_model_allowed` into Profile validation (STT + new LLM check)

**Files:**
- Modify: `apps/api_gateway/app/services/profiles/models.py`
- Modify: `apps/api_gateway/app/api/routes/profiles.py`
- Test: `tests/unit/test_profile_model_gate.py`

**Interfaces:**
- Consumes: `check_model_allowed` (Task 5); `user_store.get_by_id` (existing).
- Produces: `LlmConfig.engine: str = ""`; `_validate_stt_model` (existing function) additionally calls `check_model_allowed`; a new equivalent check for `profile.llm`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_profile_model_gate.py
import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.auth.users import user_store
from app.services.model_registry.store import ModelRegistryStore


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _signup_login(client, username: str) -> None:
    client.post("/api/auth/signup", json={"username": username, "password": "pw"})
    client.post("/api/auth/login", json={"username": username, "password": "pw"})


@pytest.mark.asyncio
async def test_disabled_llm_model_rejected_on_profile_create():
    store = ModelRegistryStore()
    await store.create("llm", "openrouter", "qwen3-asr-flash", "Qwen3 ASR Flash", stage="stable")
    created = await store.find("llm", "openrouter", "qwen3-asr-flash")
    await store.set_fields(created.id, enabled=False)


def test_profile_create_rejects_disabled_llm_engine(client, _with_password):
    import asyncio

    store = ModelRegistryStore()
    entry = asyncio.run(store.create("llm", "openrouter", "qwen3-asr-flash", "Qwen3 ASR Flash"))
    asyncio.run(store.set_fields(entry["id"], enabled=False))

    _signup_login(client, "toan")
    resp = client.post("/v1/profiles", json={
        "name": "p1",
        "llm": {"engine": "openrouter", "model": "qwen3-asr-flash", "base_url": "https://x", "api_key": ""},
    })
    assert resp.status_code == 403


def test_profile_create_allows_llm_engine_not_in_registry(client, _with_password):
    _signup_login(client, "toan")
    resp = client.post("/v1/profiles", json={
        "name": "p1",
        "llm": {"engine": "", "model": "my-self-hosted-model", "base_url": "https://x", "api_key": ""},
    })
    assert resp.status_code == 200


def test_profile_create_rejects_testing_stage_llm_for_non_tester(client, _with_password):
    import asyncio

    store = ModelRegistryStore()
    asyncio.run(store.create("llm", "openrouter", "qwen3-asr-flash", "Qwen3 ASR Flash", stage="testing"))

    _signup_login(client, "toan")
    resp = client.post("/v1/profiles", json={
        "name": "p1",
        "llm": {"engine": "openrouter", "model": "qwen3-asr-flash", "base_url": "https://x", "api_key": ""},
    })
    assert resp.status_code == 403


def test_profile_create_allows_testing_stage_llm_for_tester(client, _with_password):
    import asyncio

    from app.services.auth.users import user_store as _users

    store = ModelRegistryStore()
    asyncio.run(store.create("llm", "openrouter", "qwen3-asr-flash", "Qwen3 ASR Flash", stage="testing"))

    _signup_login(client, "toan")
    user = asyncio.run(_users.get_by_username("toan"))
    asyncio.run(_users.set_fields(user.id, can_use_testing=True))

    resp = client.post("/v1/profiles", json={
        "name": "p1",
        "llm": {"engine": "openrouter", "model": "qwen3-asr-flash", "base_url": "https://x", "api_key": ""},
    })
    assert resp.status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_profile_model_gate.py -v`
Expected: FAIL — `LlmConfig` has no `engine` field (pydantic rejects extra key, or silently ignores it and the gate never runs); `_validate_stt_model` doesn't call `check_model_allowed`

- [ ] **Step 3: Write the implementation**

In `apps/api_gateway/app/services/profiles/models.py`, add `engine` to `LlmConfig`:

```python
class LlmConfig(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    engine: str = ""
```

In `apps/api_gateway/app/api/routes/profiles.py`, add the import and make `_validate_stt_model` async, adding the two gate calls, then update its three call sites:

```python
from app.core.actor import current_role, current_user_id
from app.core.errors import AppError
from app.services.auth.users import user_store
from app.services.mcp.models import McpServer
from app.services.model_registry.gate import check_model_allowed
from app.services.profiles.models import LlmConfig, MemoryConfig, Profile, SessionConfig, SttConfig, TtsConfig
from app.services.profiles.store import profile_store
from app.services.stt.model_registry import STT_MODEL_REGISTRIES
from app.services.stt.profile import resolve_stt_profile
```

```python
async def _validate_profile_models(profile: Profile, acting_user) -> None:
    if profile.stt.model:
        preset = resolve_stt_profile(profile.stt.profile)
        engine = profile.stt.engine or (preset[0] if preset else "")
        if not engine:
            raise AppError("stt.model requires stt.engine or a resolvable stt.profile preset")
        registry = STT_MODEL_REGISTRIES.get(engine)
        if registry is None:
            raise AppError(f"engine '{engine}' has no selectable model variants")
        registry.validate(profile.stt.model)
        await check_model_allowed("stt", engine, profile.stt.model, acting_user)
    if profile.llm.engine and profile.llm.model:
        await check_model_allowed("llm", profile.llm.engine, profile.llm.model, acting_user)


async def _resolve_acting_user(request: Request):
    """None when there's no real logged-in user to resolve (dev mode, auth
    fully disabled -- see app.core.actor.current_user_id). check_model_allowed
    already handles a None user by failing closed only on the testing-stage
    check, never crashing."""
    user_id = current_user_id(request)
    return await user_store.get_by_id(user_id) if user_id else None
```

(This replaces the old synchronous `_validate_stt_model` — remove that function entirely and rename all three call sites from `_validate_stt_model(profile)` to `await _validate_profile_models(profile, acting_user)`, where `acting_user = await _resolve_acting_user(request)` is resolved once at the top of `create_profile`/`update_profile` — `get_profile`/`delete_profile`/`clone_profile` don't validate models and are unaffected.)

**IMPORTANT:** Task 1 was fixed after an initial review found two Critical bugs (cross-tenant name-collision overwrite on create/clone, and any non-admin being able to write to admin templates). By the time you're doing this task, `profiles.py` already has: an existence check (`profile_store.get(payload.name) is not None` → 409) in `create_profile`, and a `_can_write(profile, user_id, role)` predicate gating `update_profile`/`delete_profile` (not `_visible`, which stays read-only). Read the current file first and only add the two `_resolve_acting_user`/`_validate_profile_models` lines shown below to the EXISTING (already-fixed) bodies — do not paste over them with an older version. For reference, the two functions should end up looking like this:

```python
@router.post("")
async def create_profile(payload: ProfileRequest, request: Request) -> dict:
    if profile_store.get(payload.name) is not None:
        raise HTTPException(status_code=409, detail=f"'{payload.name}' already exists")
    owner_id = None if current_role(request) == "admin" else current_user_id(request)
    profile = Profile(**payload.model_dump(), owner_id=owner_id)
    acting_user = await _resolve_acting_user(request)
    await _validate_profile_models(profile, acting_user)
    profile_store.upsert(profile)
    return {"success": True, "data": _mask(profile)}
```

```python
@router.put("/{name}")
async def update_profile(name: str, payload: ProfileRequest, request: Request) -> dict:
    existing = profile_store.get(name)
    if existing is not None and not _can_write(existing, current_user_id(request), current_role(request)):
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    data = payload.model_dump()
    data["name"] = name
    data["owner_id"] = existing.owner_id if existing else (
        None if current_role(request) == "admin" else current_user_id(request)
    )
    if not data.get("llm", {}).get("api_key"):
        if existing and existing.llm.api_key:
            data.setdefault("llm", {})["api_key"] = existing.llm.api_key
    profile = Profile(**data)
    acting_user = await _resolve_acting_user(request)
    await _validate_profile_models(profile, acting_user)
    profile_store.upsert(profile)
    return {"success": True, "data": _mask(profile)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_profile_model_gate.py tests/unit/test_profile_ownership.py -v`
Expected: all pass (the second file confirms Task 1's ownership behavior wasn't broken by this refactor)

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/profiles/models.py apps/api_gateway/app/api/routes/profiles.py tests/unit/test_profile_model_gate.py
git commit -m "feat(model-registry): gate Profile's STT/LLM model choice against the registry"
```

---

## Task 7: Wire `check_model_allowed` into TtsProfile validation

**Files:**
- Modify: `apps/api_gateway/app/api/routes/tts_profiles.py`
- Test: `tests/unit/test_tts_profile_model_gate.py`

**Interfaces:**
- Consumes: `check_model_allowed` (Task 5); `user_store.get_by_id` (existing).
- Produces: `create_tts_profile`/`update_tts_profile` reject a disabled/testing-gated `engine` choice (model_id == engine, per Task 5's TTS seeding grain).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_tts_profile_model_gate.py
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.model_registry.store import ModelRegistryStore


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _signup_login(client, username: str) -> None:
    client.post("/api/auth/signup", json={"username": username, "password": "pw"})
    client.post("/api/auth/login", json={"username": username, "password": "pw"})


def test_tts_profile_create_rejects_disabled_engine(client, _with_password):
    store = ModelRegistryStore()
    entry = asyncio.run(store.create("tts", "omnivoice", "omnivoice", "OmniVoice"))
    asyncio.run(store.set_fields(entry["id"], enabled=False))

    _signup_login(client, "toan")
    resp = client.post("/v1/tts/profiles", json={"name": "p1", "engine": "omnivoice"})
    assert resp.status_code == 403


def test_tts_profile_create_allows_engine_not_in_registry(client, _with_password):
    _signup_login(client, "toan")
    resp = client.post("/v1/tts/profiles", json={"name": "p1", "engine": "some-future-engine"})
    assert resp.status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_tts_profile_model_gate.py -v`
Expected: FAIL — no gate call yet, both requests return 200

- [ ] **Step 3: Write the implementation**

In `apps/api_gateway/app/api/routes/tts_profiles.py`, add the import and validate in `create`/`update`:

```python
from app.services.auth.users import user_store
from app.services.model_registry.gate import check_model_allowed


async def _resolve_acting_user(request: Request):
    """None when there's no real logged-in user (dev mode, auth fully
    disabled). check_model_allowed handles a None user without crashing."""
    user_id = current_user_id(request)
    return await user_store.get_by_id(user_id) if user_id else None
```

**IMPORTANT:** By this point in the plan, `tts_profiles.py` already has the existence
check (`tts_profile_store.get(payload.name) is not None` → 409) in `create_tts_profile`
and `_can_write` gating `update_tts_profile` (Task 2, fixed after its own review found
the same two Critical bugs as Task 1). Read the current file first and only add the
`check_model_allowed` lines to the EXISTING bodies. For reference:

```python
@router.post("")
async def create_tts_profile(payload: TtsProfile, request: Request) -> dict:
    if tts_profile_store.get(payload.name) is not None:
        raise HTTPException(status_code=409, detail=f"'{payload.name}' already exists")
    owner_id = None if current_role(request) == "admin" else current_user_id(request)
    profile = payload.model_copy(update={"owner_id": owner_id})
    if profile.engine:
        acting_user = await _resolve_acting_user(request)
        await check_model_allowed("tts", profile.engine, profile.engine, acting_user)
    tts_profile_store.upsert(profile)
    return {"success": True, "data": profile.model_dump()}
```

```python
@router.put("/{name}")
async def update_tts_profile(name: str, payload: TtsProfile, request: Request) -> dict:
    existing = tts_profile_store.get(name)
    if not existing or not _can_write(existing, current_user_id(request), current_role(request)):
        raise HTTPException(status_code=404, detail=f"TTS profile '{name}' not found")
    data = payload.model_dump()
    data["name"] = name
    data["owner_id"] = existing.owner_id
    profile = TtsProfile(**data)
    if profile.engine:
        acting_user = await _resolve_acting_user(request)
        await check_model_allowed("tts", profile.engine, profile.engine, acting_user)
    tts_profile_store.upsert(profile)
    return {"success": True, "data": profile.model_dump()}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_tts_profile_model_gate.py tests/unit/test_tts_profile_ownership.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/api/routes/tts_profiles.py tests/unit/test_tts_profile_model_gate.py
git commit -m "feat(model-registry): gate TtsProfile's engine choice against the registry"
```

---

## Task 8: Admin `/v1/model_registry` routes (test-before-add)

**Files:**
- Create: `apps/api_gateway/app/api/routes/model_registry.py`
- Modify: `apps/api_gateway/app/main.py`
- Modify: `apps/api_gateway/app/core/auth_guard.py`
- Test: `tests/unit/test_model_registry_routes.py`

**Interfaces:**
- Consumes: `model_registry_store` (Task 5); `stt_service`/`tts_service` (existing); `OpenAICompatResponder` (`app.services.conversation.responder`, existing).
- Produces: `POST /v1/model_registry` (blocking test-before-add), `GET /v1/model_registry`, `PATCH /v1/model_registry/{id}`; `/v1/model_registry` added to `AuthGuardMiddleware`'s `_ADMIN_PREFIXES`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_model_registry_routes.py
import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.schemas.tts import TTSResult
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _signup_login(client, username: str, role: str = "user") -> None:
    client.post("/api/auth/signup", json={"username": username, "password": "pw"})
    if role == "admin":
        import asyncio

        from app.services.auth.users import user_store

        user = asyncio.run(user_store.get_by_username(username))
        asyncio.run(user_store.set_fields(user.id, role="admin"))
    client.post("/api/auth/login", json={"username": username, "password": "pw"})


class _OkStub(STTProvider):
    name = "stub-registry-ok"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None):
        from app.schemas.stt import STTResult
        return STTResult(engine=self.name, text="ok", is_final=True)


class _FailStub(STTProvider):
    name = "stub-registry-fail"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None):
        raise RuntimeError("engine unavailable")


class _TtsOkStub(TTSProvider):
    name = "stub-tts-registry-ok"

    async def synthesize(self, payload):
        return TTSResult(engine=self.name, sample_rate=24000, audio_url="/artifacts/x.wav", text=payload.text)


@pytest.fixture(autouse=True)
def _register_stubs():
    stt_service.providers["stub-registry-ok"] = _OkStub()
    stt_service.providers["stub-registry-fail"] = _FailStub()
    tts_service.providers["stub-tts-registry-ok"] = _TtsOkStub()
    yield
    stt_service.providers.pop("stub-registry-ok", None)
    stt_service.providers.pop("stub-registry-fail", None)
    tts_service.providers.pop("stub-tts-registry-ok", None)


def test_regular_user_cannot_reach_model_registry(client, _with_password):
    _signup_login(client, "toan", role="user")
    resp = client.get("/v1/model_registry")
    assert resp.status_code == 403


def test_create_stt_entry_runs_real_test_call_and_succeeds(client, _with_password):
    _signup_login(client, "root", role="admin")
    resp = client.post("/v1/model_registry", json={
        "kind": "stt", "engine": "stub-registry-ok", "model_id": "v1", "label": "Stub OK",
    })
    assert resp.status_code == 200
    assert resp.json()["data"]["enabled"] is True


def test_create_stt_entry_test_call_fails_rejects_and_does_not_persist(client, _with_password):
    _signup_login(client, "root", role="admin")
    resp = client.post("/v1/model_registry", json={
        "kind": "stt", "engine": "stub-registry-fail", "model_id": "v1", "label": "Stub Fail",
    })
    assert resp.status_code == 400
    listed = client.get("/v1/model_registry").json()["data"]
    assert not any(e["engine"] == "stub-registry-fail" for e in listed)


def test_create_tts_entry_runs_real_test_call(client, _with_password):
    _signup_login(client, "root", role="admin")
    resp = client.post("/v1/model_registry", json={
        "kind": "tts", "engine": "stub-tts-registry-ok", "model_id": "stub-tts-registry-ok",
        "label": "Stub TTS OK", "sample_text": "xin chào",
    })
    assert resp.status_code == 200


def test_patch_toggles_enabled_and_stage_without_retest(client, _with_password):
    _signup_login(client, "root", role="admin")
    created = client.post("/v1/model_registry", json={
        "kind": "stt", "engine": "stub-registry-ok", "model_id": "v1", "label": "Stub OK",
    }).json()["data"]
    resp = client.patch(f"/v1/model_registry/{created['id']}", json={"enabled": False, "stage": "testing"})
    assert resp.status_code == 200
    assert resp.json()["data"]["enabled"] is False
    assert resp.json()["data"]["stage"] == "testing"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_model_registry_routes.py -v`
Expected: FAIL with 404s (routes don't exist yet)

- [ ] **Step 3: Write the implementation**

```python
# apps/api_gateway/app/api/routes/model_registry.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.schemas.tts import TTSRequest
from app.services.conversation.responder import OpenAICompatResponder
from app.services.model_registry.store import model_registry_store
from app.services.stt.service import stt_service
from app.services.tts.service import tts_service

router = APIRouter(prefix="/v1/model_registry", tags=["model_registry"])

# Short silence buffer, same shape as other STT test fixtures in this codebase
# (raw 16-bit PCM, mono) -- enough to exercise the provider without needing a
# real recorded sample.
_SAMPLE_PCM16 = b"\x00\x00" * 1600


class CreateEntryRequest(BaseModel):
    kind: str
    engine: str
    model_id: str
    label: str
    stage: str = "stable"
    base_url: str = ""
    api_key: str = ""
    sample_text: str = "xin chào"


class UpdateEntryRequest(BaseModel):
    enabled: bool | None = None
    stage: str | None = None


@router.get("")
async def list_entries() -> dict:
    return {"success": True, "data": await model_registry_store.list_all()}


@router.post("")
async def create_entry(payload: CreateEntryRequest) -> dict:
    try:
        if payload.kind == "stt":
            provider = stt_service.get_provider(payload.engine)
            await provider.transcribe_bytes(_SAMPLE_PCM16)
        elif payload.kind == "tts":
            provider = tts_service.get_provider(payload.engine)
            await provider.synthesize(TTSRequest(text=payload.sample_text, engine=payload.engine))
        elif payload.kind == "llm":
            responder = OpenAICompatResponder(
                base_url=payload.base_url, api_key=payload.api_key, model=payload.model_id,
                system_prompt="", timeout=30.0,
            )
            await responder.reply([{"role": "user", "content": payload.sample_text}])
        else:
            raise HTTPException(status_code=400, detail=f"unknown kind '{payload.kind}'")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - surface the provider's own error to the admin
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    created = await model_registry_store.create(
        payload.kind, payload.engine, payload.model_id, payload.label, stage=payload.stage
    )
    return {"success": True, "data": created}


@router.patch("/{entry_id}")
async def update_entry(entry_id: str, payload: UpdateEntryRequest) -> dict:
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    updated = await model_registry_store.set_fields(entry_id, **fields)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"model registry entry '{entry_id}' not found")
    return {"success": True, "data": updated}
```

In `apps/api_gateway/app/main.py`, add the import and registration alongside the other routers:

```python
from app.api.routes.model_registry import router as model_registry_router
```

```python
app.include_router(model_registry_router)
```

In `apps/api_gateway/app/core/auth_guard.py`, add `"/v1/model_registry"` to `_ADMIN_PREFIXES`:

```python
_ADMIN_PREFIXES = ("/v1/system", "/v1/models", "/v1/users", "/v1/devices", "/v1/model_registry")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_model_registry_routes.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/api/routes/model_registry.py apps/api_gateway/app/main.py apps/api_gateway/app/core/auth_guard.py tests/unit/test_model_registry_routes.py
git commit -m "feat(model-registry): add admin routes with blocking test-before-add"
```

---

## Task 9: UI — Clone action + "mine" badge (Profile/TtsProfile/MCP)

**Files:**
- Modify: `apps/api_gateway/app/static/js/profiles.js`
- Modify: `apps/api_gateway/app/static/js/mcp-servers.js`
- Modify: `apps/api_gateway/app/static/js/tts-profiles.js` (create if the codebase doesn't already have a dedicated file — check first: `grep -rl "tts/profiles" apps/api_gateway/app/static/js/`)

**Interfaces:**
- Consumes: `POST /v1/profiles/{name}/clone`, `POST /v1/tts/profiles/{name}/clone`, `POST /v1/mcp/servers/{name}/clone` (Tasks 1-2); `fetchAuthStatus()` (`app.static.js.session`, existing from the identity/auth branch).
- Produces: a "Clone" button per row in each of the three lists; owned (non-template) rows get a small "mine" badge; template rows a non-admin can see show Clone only (no Edit/Delete button rendered — the server 404s those anyway, this just avoids a pointless round trip).

No JS test tooling in this repo — verified by careful reading, matching the established pattern from the earlier identity/auth UI tasks.

- [ ] **Step 1: Write the implementation**

Read the current row-rendering function in each of the three files first (`renderMcpList` in `mcp-servers.js` is the reference pattern already read in this plan's research phase). For each, add:
1. An `owner_id` check when rendering each row: if `row.owner_id` is truthy, add a `<span class="hint">mine</span>` badge; if the row is a template (`owner_id` is `null`) and the current user isn't an admin (fetch via `fetchAuthStatus()`, cached), hide the row's Edit/Delete buttons, showing only Clone.
2. A `data-*-clone="<name>"` button per row, wired to a handler that `prompt()`s for a new name, then `POST`s to that resource's `/{name}/clone` endpoint with `{new_name}`, and reloads the list on success (mirroring the existing `addMcpServer`/create-flow error-handling shape: on non-2xx, show `body.detail` via the existing `print()` helper).

Apply the same shape to `profiles.js`'s profile-list rendering and to the TTS profiles list (in whichever file currently renders it — grep first, since the plan's research pass didn't confirm whether TtsProfile list rendering lives in a dedicated `tts-profiles.js` or is inlined elsewhere).

- [ ] **Step 2: Manually verify**

With a running server (`ADMIN_PASSWORD` set):
1. As admin, create a template Profile/TtsProfile/MCP server each.
2. Log in as a regular user — each list shows the template with only a Clone button (no Edit/Delete).
3. Click Clone on each, enter a new name — the row appears in the list with a "mine" badge and now has Edit/Delete too.
4. Log in as a second regular user — confirm the first user's cloned (owned) rows are absent from their list, but the original templates are still visible.

- [ ] **Step 3: Commit**

```bash
git add apps/api_gateway/app/static/js/profiles.js apps/api_gateway/app/static/js/mcp-servers.js apps/api_gateway/app/static/js/tts-profiles.js
git commit -m "feat(ui): add clone-from-template + ownership badge to Profile/TtsProfile/MCP lists"
```

---

## Task 10: UI — Model Registry admin page

**Files:**
- Create: `apps/api_gateway/app/static/js/model-registry.js`
- Modify: `apps/api_gateway/app/static/index.html`
- Modify: `apps/api_gateway/app/static/js/sidebar-nav.js`
- Modify: `apps/api_gateway/app/static/js/main.js`

**Interfaces:**
- Consumes: `GET/POST/PATCH /v1/model_registry` (Task 8).
- Produces: a "Model Registry" nav item (admin-only, `.admin-only` class per the identity/auth branch's established pattern), a table of entries with enabled/stage controls, and an "Add entry" form matching `CreateEntryRequest` (kind-dependent fields, and a "Testing…" pending state while the blocking test call is in flight).

No JS test tooling in this repo — verified by careful reading + a live smoke test, matching the established pattern from the identity/auth branch's Devices tab (Task 19 of that plan).

- [ ] **Step 1: Write the implementation**

```js
// apps/api_gateway/app/static/js/model-registry.js
import { el, print } from "./helpers.js";

export let registryData = [];

function _escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML;
}

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
  if (!registryData.length) {
    host.innerHTML = '<p class="hint">No entries yet.</p>';
    return;
  }
  host.innerHTML = registryData.map((e) => `
    <div class="model-row ${e.enabled ? "" : "dim"}">
      <div class="model-info">
        <strong>${_escapeHtml(e.kind)}</strong>
        <code>${_escapeHtml(e.engine)}/${_escapeHtml(e.model_id)}</code>
        <span class="hint">${_escapeHtml(e.label)}</span>
        <select data-registry-stage="${e.id}">
          <option value="stable" ${e.stage === "stable" ? "selected" : ""}>stable</option>
          <option value="testing" ${e.stage === "testing" ? "selected" : ""}>testing</option>
        </select>
      </div>
      <div class="model-action">
        <button class="mini" data-registry-toggle="${e.id}">${e.enabled ? "Disable" : "Enable"}</button>
      </div>
    </div>
  `).join("");

  document.querySelectorAll("[data-registry-stage]").forEach((sel) =>
    sel.addEventListener("change", () =>
      patchEntry(sel.getAttribute("data-registry-stage"), { stage: sel.value })
    )
  );
  document.querySelectorAll("[data-registry-toggle]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-registry-toggle");
      const entry = registryData.find((e) => e.id === id);
      patchEntry(id, { enabled: !entry.enabled });
    })
  );
}

async function patchEntry(id, fields) {
  try {
    const resp = await fetch(`/v1/model_registry/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(fields),
    });
    if (!resp.ok) {
      const body = await resp.json();
      print(el("model-registry-status"), body.detail || "Update failed", true);
      return;
    }
    await loadModelRegistry();
  } catch (error) {
    print(el("model-registry-status"), String(error), true);
  }
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

Add to `apps/api_gateway/app/static/index.html`'s sidebar `<nav>` list, an admin-only item (following the exact `.admin-only` + `data-section` pattern the identity/auth branch established for the "Users" item — place it after "Users", before "Models"):

```html
            <li class="admin-only">
              <button class="nav-item" data-section="model-registry">
                <span class="nav-icon">&#9636;</span>
                <span class="nav-label">Model Registry</span>
              </button>
            </li>
```

Add the section markup before `<div class="section" id="section-models">`:

```html
          <!-- ============================== MODEL REGISTRY ============================== -->
          <div class="section" id="section-model-registry">
            <section class="card">
              <div class="card-head">
                <h2>Model Registry</h2>
                <button id="model-registry-refresh" class="ghost mini">Refresh</button>
              </div>
              <p class="hint">Control which STT/TTS/LLM models users may select, and gate "testing" models to accounts with testing access.</p>
              <div id="model-registry-list" class="model-list">
                <p class="hint">Loading&#8230;</p>
              </div>
              <h3 class="sub">Add Entry</h3>
              <div class="row tight">
                <label>
                  Kind
                  <select id="registry-add-kind">
                    <option value="stt">stt</option>
                    <option value="tts">tts</option>
                    <option value="llm">llm</option>
                  </select>
                </label>
                <label>
                  Engine
                  <input id="registry-add-engine" type="text" placeholder="openrouter" />
                </label>
                <label>
                  Model ID
                  <input id="registry-add-model-id" type="text" placeholder="qwen3-asr-flash" />
                </label>
                <label>
                  Label
                  <input id="registry-add-label" type="text" placeholder="Qwen3 ASR Flash" />
                </label>
                <label>
                  Stage
                  <select id="registry-add-stage">
                    <option value="stable">stable</option>
                    <option value="testing">testing</option>
                  </select>
                </label>
              </div>
              <div class="row tight hidden" id="registry-add-llm-fields">
                <label>
                  Base URL
                  <input id="registry-add-base-url" type="text" placeholder="https://openrouter.ai/api/v1" />
                </label>
                <label>
                  API Key
                  <input id="registry-add-api-key" type="password" />
                </label>
              </div>
              <div class="actions end">
                <button id="registry-add-btn">Add (runs a live test first)</button>
              </div>
              <p id="model-registry-status" class="meta"></p>
            </section>
          </div>
```

Add the sidebar case in `apps/api_gateway/app/static/js/sidebar-nav.js` (alongside the existing `if (section === "users") loadUsers();` line):

```js
if (section === "model-registry") loadModelRegistry();
```

(and the import: `import { loadModelRegistry } from "./model-registry.js";`)

Add `import "./model-registry.js";` to `apps/api_gateway/app/static/js/main.js`'s side-effect-only import block.

- [ ] **Step 2: Manually verify**

With a running server (`ADMIN_PASSWORD` set) and a stub/local OpenAI-compatible endpoint (or a real one if available):
1. As admin, open "Model Registry" — the list shows the seeded STT/TTS entries from Task 5.
2. Toggle an entry's Enable/Disable — the row updates immediately.
3. Add a new `llm` entry pointing at a working endpoint — status shows "Testing…" then "Added". The entry appears in the list.
4. Add a new `llm` entry pointing at an unreachable `base_url` — status shows the failure, no row is added.
5. As a regular (non-admin) user, confirm the "Model Registry" tab is absent from the sidebar.

- [ ] **Step 3: Commit**

```bash
git add apps/api_gateway/app/static/js/model-registry.js apps/api_gateway/app/static/index.html apps/api_gateway/app/static/js/sidebar-nav.js apps/api_gateway/app/static/js/main.js
git commit -m "feat(ui): add Model Registry admin page"
```

## Final Verification

- [ ] Run the full scoped suite: `pytest tests/unit tests/integration -q`. Expected: all new tests pass, plus every pre-existing test still green (one pre-existing, unrelated failure is expected: `tests/unit/test_conversation_engine_ready.py::test_session_started_reports_ready_when_already_warm`).
- [ ] Manually walk: admin creates a template Profile/TtsProfile/MCP server → a regular user clones each → a second regular user can't see the first user's clones → admin adds a Model Registry entry for a real engine and disables it → a profile referencing that now-disabled engine/model is rejected on save → admin re-enables it → save succeeds.
