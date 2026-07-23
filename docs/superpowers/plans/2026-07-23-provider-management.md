# Provider Management Implementation Plan (Phase 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cấu hình endpoint + api_key của một provider (OpenAI / OpenRouter / QwenCloud) **một lần**, rồi model trong Model Registry tham chiếu provider đó thay vì lặp lại credential mỗi dòng.

**Architecture:** Bảng `providers` mới (id, name, base_url, api_key, enabled, config). Model Registry entry liên kết provider qua `config.provider_id` (JSON có sẵn — **không đổi schema bảng cũ**). Một hàm resolver duy nhất trả `(base_url, api_key)` cho một entry: có `provider_id` → lấy từ provider; không có → dùng field cũ của entry (fallback cho ollama/engine local — chạy y nguyên). LLM responder + STT/TTS service-provider + test-call lúc tạo model đều đi qua resolver này.

**Tech Stack:** FastAPI, SQLAlchemy async (`db_session`), SQLite (create_all), pytest + pytest-asyncio + `fastapi.testclient.TestClient`, httpx.

**Spec:** `docs/superpowers/specs/2026-07-23-provider-management-usage-quota-design.md` (§3, §8, §9, §10). Usage/Pricing = Plan 2; Quota = Plan 3 (ngoài phạm vi file này).

## Global Constraints

- Python 3.12 venv (`.venv`); 3.14 thiếu wheels ML — không dùng.
- **Test layout (repo-specific — briefs MUST honor this over any boilerplate below):** tests sống ở repo-root `tests/unit/`; chạy từ repo root bằng `.venv/bin/python -m pytest tests/unit/<file> -v`. `pyproject.toml` đặt `asyncio_mode="auto"` (KHÔNG cần `@pytest.mark.asyncio`) và `_tmp_db` là fixture **`autouse=True`** — **KHÔNG bao giờ nhận `_tmp_db` làm tham số** của hàm test (nó tự chạy, cấp DB tmp per-test). `TestClient` dùng bản đồng bộ. KHÔNG chạy test trong submodule.
- **Không ALTER** bảng cũ. Bảng mới `providers` do `Base.metadata.create_all` tự tạo (`db/engine.py::init_db`).
- `api_key` phải **mask** trong mọi response API (dùng `_mask_api_key` sẵn có ở `routes/model_registry.py`).
- Provider linkage lưu ở `entry["config"]["provider_id"]`; rỗng/absent = fallback về `entry.base_url`/`entry.api_key`.
- Router provider dùng prefix `/v1/providers`; gate admin bằng cách thêm prefix vào `_ADMIN_PREFIXES` (`core/auth_guard.py`).
- Commit sau mỗi task. **KHÔNG push** (main auto-deploy prod — người dùng tự quyết định push).
- Git identity: `lugondev <lugondev@gmail.com>`.
- Gemini **để lại** (native API khác OpenAI-compat) — Phase 1 chỉ 3 provider OpenAI-compatible.

---

### Task 1: `Provider` DB model

**Files:**
- Modify: `apps/api_gateway/app/services/db/models.py` (thêm class sau `ModelRegistryEntry`, kết thúc dòng 117)
- Test: `tests/unit/test_provider_model.py`

**Interfaces:**
- Produces: `Provider` ORM model, table `providers`, cột `id, name, label, base_url, api_key, enabled, config`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_provider_model.py
import pytest
from sqlalchemy import select

from app.services.db.engine import db_session, init_db
from app.services.db.models import Provider


@pytest.mark.asyncio
async def test_provider_row_roundtrips():
    await init_db()
    async with db_session() as s:
        s.add(Provider(id="p1", name="openai", label="OpenAI",
                       base_url="https://api.openai.com/v1", api_key="sk-x", enabled=True))
        await s.commit()
    async with db_session() as s:
        row = (await s.execute(select(Provider))).scalars().one()
    assert row.name == "openai"
    assert row.base_url == "https://api.openai.com/v1"
    assert row.api_key == "sk-x"
    assert row.enabled is True
    assert row.config == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_provider_model.py -v`
Expected: FAIL — `ImportError: cannot import name 'Provider'`.

- [ ] **Step 3: Add the model**

```python
# apps/api_gateway/app/services/db/models.py  (append after ModelRegistryEntry)
class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # Provider family: "openai" | "openrouter" | "qwencloud" | custom string.
    name: Mapped[str] = mapped_column(String(64), index=True)
    label: Mapped[str] = mapped_column(String(128), default="")
    # OpenAI-compatible base URL (ends with /v1). Shared by every registry entry
    # whose config.provider_id points here.
    base_url: Mapped[str] = mapped_column(String(256), default="")
    api_key: Mapped[str] = mapped_column(String(256), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Extra per-provider knobs (default timeout, org id, extra headers). Free-form.
    config: Mapped[dict] = mapped_column(JSON, default=dict)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_provider_model.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/db/models.py tests/unit/test_provider_model.py
git -c user.name=lugondev -c user.email=lugondev@gmail.com commit -m "feat(providers): add Provider DB model"
```

---

### Task 2: `ProviderStore` (in-memory cache + write-through) + conftest invalidation

**Files:**
- Create: `apps/api_gateway/app/services/providers/__init__.py` (empty)
- Create: `apps/api_gateway/app/services/providers/store.py`
- Modify: `tests/conftest.py:127-142` (invalidate provider_store alongside model_registry_store)
- Test: `tests/unit/test_provider_store.py`

**Interfaces:**
- Consumes: `Provider` (Task 1), `db_session` (`app.services.db.engine`).
- Produces: singleton `provider_store` with:
  - `async list_all() -> list[dict]`
  - `async get(provider_id: str) -> dict | None`
  - `get_sync(provider_id: str) -> dict | None`  (cache-only; None if cache cold — callers treat as "no provider")
  - `async create(name, label="", base_url="", api_key="", enabled=True, config=None) -> dict`
  - `async set_fields(provider_id, **fields) -> dict | None`
  - `async delete(provider_id) -> bool`
  - `invalidate() -> None`
  - Each dict: `{"id","name","label","base_url","api_key","enabled","config"}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_provider_store.py
import pytest

from app.services.db.engine import init_db
from app.services.providers.store import provider_store


@pytest.mark.asyncio
async def test_create_get_and_sync_readback():
    await init_db()
    created = await provider_store.create(
        name="openrouter", label="OpenRouter",
        base_url="https://openrouter.ai/api/v1", api_key="sk-or-x",
    )
    pid = created["id"]
    assert created["name"] == "openrouter"

    got = await provider_store.get(pid)
    assert got["api_key"] == "sk-or-x"

    # sync path (used off the event loop) sees the same cached row
    assert provider_store.get_sync(pid)["base_url"] == "https://openrouter.ai/api/v1"


@pytest.mark.asyncio
async def test_set_fields_and_delete():
    await init_db()
    created = await provider_store.create(name="openai", base_url="x", api_key="k")
    pid = created["id"]

    updated = await provider_store.set_fields(pid, api_key="k2", enabled=False)
    assert updated["api_key"] == "k2"
    assert updated["enabled"] is False

    assert await provider_store.delete(pid) is True
    assert await provider_store.get(pid) is None
    assert provider_store.get_sync(pid) is None


@pytest.mark.asyncio
async def test_get_sync_returns_none_when_cache_cold():
    provider_store.invalidate()
    assert provider_store.get_sync("anything") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_provider_store.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.providers.store`.

- [ ] **Step 3: Implement the store** (mirror `ModelRegistryStore`, `services/model_registry/store.py`)

```python
# apps/api_gateway/app/services/providers/store.py
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select

from app.services.db.engine import db_session
from app.services.db.models import Provider


def _entry_dict(p: Provider) -> dict:
    return {
        "id": p.id, "name": p.name, "label": p.label, "base_url": p.base_url,
        "api_key": p.api_key, "enabled": p.enabled, "config": p.config or {},
    }


def _copy(entry: dict) -> dict:
    """Detached copy: routes mask api_key on what they get back, and mutating
    the cached object would corrupt the real key for later get_sync() calls."""
    out = dict(entry)
    out["config"] = dict(entry["config"])
    return out


class ProviderStore:
    """In-memory cache (keyed by id) + write-through to DB. Same pattern as
    ModelRegistryStore: `get_sync` serves off-event-loop callers (provider
    build via asyncio.to_thread) and returns None when the cache is cold."""

    def __init__(self) -> None:
        self._by_id: dict[str, dict] | None = None
        self._lock = asyncio.Lock()

    async def _ensure_loaded(self) -> None:
        if self._by_id is not None:
            return
        async with self._lock:
            if self._by_id is not None:
                return
            async with db_session() as s:
                rows = (await s.execute(select(Provider))).scalars().all()
            self._by_id = {p.id: _entry_dict(p) for p in rows}

    def invalidate(self) -> None:
        self._by_id = None
        self._lock = asyncio.Lock()

    async def list_all(self) -> list[dict]:
        await self._ensure_loaded()
        entries = sorted(self._by_id.values(), key=lambda e: (e["name"], e["id"]))
        return [_copy(e) for e in entries]

    async def get(self, provider_id: str) -> dict | None:
        await self._ensure_loaded()
        entry = self._by_id.get(provider_id)
        return None if entry is None else _copy(entry)

    def get_sync(self, provider_id: str) -> dict | None:
        by_id = self._by_id
        if by_id is None:
            return None
        entry = by_id.get(provider_id)
        return None if entry is None else _copy(entry)

    async def create(self, name: str, label: str = "", base_url: str = "",
                     api_key: str = "", enabled: bool = True,
                     config: dict | None = None) -> dict:
        await self._ensure_loaded()
        async with db_session() as s:
            row = Provider(id=str(uuid.uuid4()), name=name, label=label, base_url=base_url,
                           api_key=api_key, enabled=enabled, config=config or {})
            s.add(row)
            await s.commit()
            entry = _entry_dict(row)
        self._by_id[entry["id"]] = entry
        return _copy(entry)

    async def set_fields(self, provider_id: str, **fields) -> dict | None:
        await self._ensure_loaded()
        async with db_session() as s:
            row = await s.get(Provider, provider_id)
            if row is None:
                return None
            for key, value in fields.items():
                setattr(row, key, value)
            await s.commit()
            entry = _entry_dict(row)
        self._by_id[provider_id] = entry
        return _copy(entry)

    async def delete(self, provider_id: str) -> bool:
        await self._ensure_loaded()
        async with db_session() as s:
            row = await s.get(Provider, provider_id)
            if row is None:
                return False
            await s.delete(row)
            await s.commit()
        self._by_id.pop(provider_id, None)
        return True


provider_store = ProviderStore()
```

- [ ] **Step 4: Wire test isolation in conftest**

In `tests/conftest.py`, inside `_tmp_db` (after line 127 import block and the existing `model_registry_store.invalidate()` calls), add provider_store invalidation so a cache warmed against a prior test's DB never leaks:

```python
    from app.services.providers.store import provider_store   # add near line 127
```
```python
    model_registry_store.invalidate()
    provider_store.invalidate()                                # add after line 138 (the yield-side one too)
```
Add `provider_store.invalidate()` in BOTH places `model_registry_store.invalidate()` appears (before `yield` at ~138 and after at ~142).

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_provider_store.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/services/providers/ tests/unit/test_provider_store.py tests/conftest.py
git -c user.name=lugondev -c user.email=lugondev@gmail.com commit -m "feat(providers): ProviderStore cache + write-through, test isolation"
```

---

### Task 3: Credential resolver (`resolve_credentials` sync + async)

**Files:**
- Create: `apps/api_gateway/app/services/providers/resolve.py`
- Test: `tests/unit/test_provider_resolve.py`

**Interfaces:**
- Consumes: `provider_store` (Task 2).
- Produces:
  - `resolve_credentials_sync(entry: dict) -> tuple[str, str]`  → `(base_url, api_key)`
  - `async resolve_credentials(entry: dict) -> tuple[str, str]`
  - Rule: `entry["config"].get("provider_id")` truthy AND provider found → provider's `(base_url, api_key)`; else `(entry.get("base_url",""), entry.get("api_key",""))`.
  - `PROVIDER_PRESETS: list[dict]` (name/label/base_url defaults for the 3 supported providers).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_provider_resolve.py
import pytest

from app.services.db.engine import init_db
from app.services.providers.resolve import (
    resolve_credentials, resolve_credentials_sync, PROVIDER_PRESETS,
)
from app.services.providers.store import provider_store


@pytest.mark.asyncio
async def test_uses_provider_when_linked():
    await init_db()
    p = await provider_store.create(name="openai", base_url="https://api.openai.com/v1", api_key="sk-P")
    entry = {"base_url": "", "api_key": "", "config": {"provider_id": p["id"]}}
    assert await resolve_credentials(entry) == ("https://api.openai.com/v1", "sk-P")
    assert resolve_credentials_sync(entry) == ("https://api.openai.com/v1", "sk-P")


@pytest.mark.asyncio
async def test_falls_back_to_entry_when_no_provider():
    await init_db()
    entry = {"base_url": "http://localhost:11434/v1", "api_key": "", "config": {}}
    assert await resolve_credentials(entry) == ("http://localhost:11434/v1", "")
    assert resolve_credentials_sync(entry) == ("http://localhost:11434/v1", "")


@pytest.mark.asyncio
async def test_falls_back_when_provider_id_dangling():
    await init_db()
    entry = {"base_url": "http://x/v1", "api_key": "k", "config": {"provider_id": "missing"}}
    assert await resolve_credentials(entry) == ("http://x/v1", "k")


def test_presets_cover_three_providers():
    names = {p["name"] for p in PROVIDER_PRESETS}
    assert {"openai", "openrouter", "qwencloud"} <= names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_provider_resolve.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement the resolver**

```python
# apps/api_gateway/app/services/providers/resolve.py
from __future__ import annotations

from app.services.providers.store import provider_store

# Prefilled base_url defaults for the 3 supported OpenAI-compatible providers.
# EDITABLE in the UI. NOTE: verify the QwenCloud/DashScope compatible-mode URL
# against current Alibaba Cloud docs before shipping (int'l vs CN endpoints
# differ, e.g. dashscope-intl.* vs dashscope.*). Do not treat as authoritative.
PROVIDER_PRESETS: list[dict] = [
    {"name": "openai", "label": "OpenAI", "base_url": "https://api.openai.com/v1"},
    {"name": "openrouter", "label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1"},
    {"name": "qwencloud", "label": "Qwen Cloud (DashScope)",
     "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"},
]


def _provider_id(entry: dict) -> str:
    return (entry.get("config") or {}).get("provider_id") or ""


def _from_provider(provider: dict | None, entry: dict) -> tuple[str, str]:
    if provider:
        return provider["base_url"], provider["api_key"]
    return entry.get("base_url", ""), entry.get("api_key", "")


def resolve_credentials_sync(entry: dict) -> tuple[str, str]:
    """(base_url, api_key) for a registry entry, cache-only. If config.provider_id
    resolves to a provider, use it; else fall back to the entry's own fields."""
    pid = _provider_id(entry)
    provider = provider_store.get_sync(pid) if pid else None
    return _from_provider(provider, entry)


async def resolve_credentials(entry: dict) -> tuple[str, str]:
    pid = _provider_id(entry)
    provider = await provider_store.get(pid) if pid else None
    return _from_provider(provider, entry)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_provider_resolve.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/providers/resolve.py tests/unit/test_provider_resolve.py
git -c user.name=lugondev -c user.email=lugondev@gmail.com commit -m "feat(providers): credential resolver + provider presets"
```

---

### Task 4: `/v1/providers` CRUD routes + admin gate + register

**Files:**
- Create: `apps/api_gateway/app/api/routes/providers.py`
- Modify: `apps/api_gateway/app/main.py` (import + `include_router`, mirror model_registry lines 22 & 262)
- Modify: `apps/api_gateway/app/core/auth_guard.py:43` (add `/v1/providers` to `_ADMIN_PREFIXES`)
- Test: `tests/unit/test_providers_routes.py`

**Interfaces:**
- Consumes: `provider_store` (Task 2), `PROVIDER_PRESETS` (Task 3), `_mask_api_key` (copy the helper — it's a 6-line pure fn; do NOT import from model_registry route to avoid coupling two route modules).
- Produces: `router` (prefix `/v1/providers`): `GET ""`, `GET "/presets"`, `POST ""`, `PATCH "/{id}"`, `DELETE "/{id}"`. All return `{"success": True, "data": ...}`, api_key masked.

- [ ] **Step 1: Write the failing test** (reuse the admin-login helper pattern from `tests/unit/test_model_registry_routes.py`)

```python
# tests/unit/test_providers_routes.py
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


def _login_admin(client, username="adm"):
    import asyncio
    from app.services.auth.users import user_store
    client.post("/api/auth/signup", json={"username": username, "password": "pw"})
    user = asyncio.run(user_store.get_by_username(username))
    asyncio.run(user_store.set_fields(user.id, role="admin"))
    client.post("/api/auth/login", json={"username": username, "password": "pw"})


def test_regular_user_cannot_reach_providers(client, _with_password):
    client.post("/api/auth/signup", json={"username": "bob", "password": "pw"})
    client.post("/api/auth/login", json={"username": "bob", "password": "pw"})
    assert client.get("/v1/providers").status_code == 403


def test_admin_crud_and_key_masking(client, _with_password):
    _login_admin(client)
    # create
    resp = client.post("/v1/providers", json={
        "name": "openrouter", "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1", "api_key": "sk-or-v1-abcdefghijklmno",
    })
    assert resp.status_code == 200, resp.text
    created = resp.json()["data"]
    assert created["api_key"] != "sk-or-v1-abcdefghijklmno"   # masked
    assert "..." in created["api_key"]

    # list also masks
    listed = client.get("/v1/providers").json()["data"]
    assert any(p["id"] == created["id"] for p in listed)
    assert all("sk-or-v1-abcdefghijklmno" != p["api_key"] for p in listed)

    # patch with blank api_key keeps existing (no unmasking needed to test here)
    r = client.patch(f"/v1/providers/{created['id']}", json={"enabled": False, "api_key": ""})
    assert r.json()["data"]["enabled"] is False

    # delete
    assert client.delete(f"/v1/providers/{created['id']}").json()["data"]["deleted"] is True


def test_presets_endpoint(client, _with_password):
    _login_admin(client)
    data = client.get("/v1/providers/presets").json()["data"]
    assert {p["name"] for p in data} >= {"openai", "openrouter", "qwencloud"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_providers_routes.py -v`
Expected: FAIL — 404 (router not registered) / import error.

- [ ] **Step 3: Implement the router**

```python
# apps/api_gateway/app/api/routes/providers.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.providers.resolve import PROVIDER_PRESETS
from app.services.providers.store import provider_store

router = APIRouter(prefix="/v1/providers", tags=["providers"])


def _mask_api_key(key: str) -> str:
    """Partial reveal so an admin can tell which key is which at a glance
    (same convention as routes/model_registry.py::_mask_api_key)."""
    if not key:
        return ""
    if len(key) <= 15:
        return "***"
    return f"{key[:12]}...{key[-3:]}"


def _masked(entry: dict) -> dict:
    entry = dict(entry)
    entry["api_key"] = _mask_api_key(entry["api_key"])
    return entry


class CreateProviderRequest(BaseModel):
    name: str
    label: str = ""
    base_url: str = ""
    api_key: str = ""
    enabled: bool = True
    config: dict = {}


class UpdateProviderRequest(BaseModel):
    name: str | None = None
    label: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    enabled: bool | None = None
    config: dict | None = None


@router.get("")
async def list_providers() -> dict:
    return {"success": True, "data": [_masked(p) for p in await provider_store.list_all()]}


@router.get("/presets")
async def list_presets() -> dict:
    return {"success": True, "data": PROVIDER_PRESETS}


@router.post("")
async def create_provider(payload: CreateProviderRequest) -> dict:
    created = await provider_store.create(
        name=payload.name, label=payload.label, base_url=payload.base_url,
        api_key=payload.api_key, enabled=payload.enabled, config=payload.config,
    )
    return {"success": True, "data": _masked(created)}


@router.patch("/{provider_id}")
async def update_provider(provider_id: str, payload: UpdateProviderRequest) -> dict:
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    # Blank api_key means "keep existing" -- same convention as the secret
    # fields in routes/model_registry.py (UI never pre-fills a real key).
    if "api_key" in fields and not fields["api_key"]:
        del fields["api_key"]
    updated = await provider_store.set_fields(provider_id, **fields)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"provider '{provider_id}' not found")
    return {"success": True, "data": _masked(updated)}


@router.delete("/{provider_id}")
async def delete_provider(provider_id: str) -> dict:
    if not await provider_store.delete(provider_id):
        raise HTTPException(status_code=404, detail=f"provider '{provider_id}' not found")
    return {"success": True, "data": {"id": provider_id, "deleted": True}}
```

- [ ] **Step 4: Register router + admin gate**

In `apps/api_gateway/app/main.py`:
```python
from app.api.routes.providers import router as providers_router   # near line 23
```
```python
app.include_router(providers_router)                              # near line 262, after model_registry_router
```
In `apps/api_gateway/app/core/auth_guard.py:43`:
```python
_ADMIN_PREFIXES = ("/v1/system", "/v1/models", "/v1/users", "/v1/devices", "/v1/model_registry", "/v1/providers")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_providers_routes.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/api/routes/providers.py apps/api_gateway/app/main.py apps/api_gateway/app/core/auth_guard.py tests/unit/test_providers_routes.py
git -c user.name=lugondev -c user.email=lugondev@gmail.com commit -m "feat(providers): /v1/providers admin CRUD + presets endpoint"
```

---

### Task 5: Model Registry test-call uses provider credentials

**Files:**
- Modify: `apps/api_gateway/app/api/routes/model_registry.py` (create_entry, ~lines 227-252; helper near top)
- Test: `tests/unit/test_model_registry_provider_link.py`

**Interfaces:**
- Consumes: `resolve_credentials` (Task 3).
- Behavior: when `payload.config` contains a `provider_id`, the add-time test-call (`OpenAICompatResponder` for llm; `OpenRouterSttProvider`/`HttpSttProvider`; `HttpTtsProvider`) uses the **resolved** `(base_url, api_key)` from that provider, so the admin does not re-type credentials. Persisted entry keeps `config.provider_id`; its own `base_url`/`api_key` may stay blank.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_model_registry_provider_link.py
import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.conversation.responder import OpenAICompatResponder


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


def test_llm_entry_test_call_uses_provider_creds(client, _with_password, monkeypatch):
    _login_admin(client)
    # a provider with the real endpoint + key
    prov = client.post("/v1/providers", json={
        "name": "openrouter", "base_url": "https://openrouter.ai/api/v1", "api_key": "sk-or-REAL",
    }).json()["data"]

    captured = {}

    async def fake_reply(self, history):
        captured["base_url"] = self.base_url
        captured["api_key"] = self.api_key
        return "ok"

    async def fake_close(self):
        return None

    monkeypatch.setattr(OpenAICompatResponder, "reply", fake_reply)
    monkeypatch.setattr(OpenAICompatResponder, "aclose", fake_close)

    # create an llm model that references the provider, leaving base_url/api_key blank
    resp = client.post("/v1/model_registry", json={
        "kind": "llm", "engine": "openrouter", "model_id": "qwen/qwen-2.5-72b-instruct",
        "label": "Qwen 2.5 72B", "config": {"provider_id": prov["id"]},
    })
    assert resp.status_code == 200, resp.text
    assert captured["base_url"] == "https://openrouter.ai/api/v1"
    assert captured["api_key"] == "sk-or-REAL"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_model_registry_provider_link.py -v`
Expected: FAIL — `captured["api_key"] == ""` (currently uses `payload.api_key`, which is blank).

- [ ] **Step 3: Resolve creds in create_entry**

In `apps/api_gateway/app/api/routes/model_registry.py`, add import near the other service imports (top of file):
```python
from app.services.providers.resolve import resolve_credentials
```
Inside `create_entry`, immediately after `_validate_known_engine(...)` and the not-installed guard (before the `try:` at ~line 227), compute effective creds:
```python
    # If the entry links a provider (config.provider_id), the add-time test-call
    # and the persisted lookup path both use the provider's shared base_url/api_key
    # so the admin need not retype credentials per model.
    eff_base_url, eff_api_key = await resolve_credentials(payload.model_dump())
```
Then replace the four uses of `payload.api_key` / `payload.base_url` inside the test-call block with `eff_api_key` / `eff_base_url`:
- `OpenRouterSttProvider(..., api_key=eff_api_key)`
- `HttpSttProvider(name=payload.engine, entry={**payload.model_dump(), "base_url": eff_base_url, "api_key": eff_api_key})`
- `HttpTtsProvider(name=payload.engine, entry={**payload.model_dump(), "base_url": eff_base_url, "api_key": eff_api_key})`
- `OpenAICompatResponder(base_url=eff_base_url, api_key=eff_api_key, model=payload.model_id, system_prompt="", timeout=30.0)`

Leave the final `model_registry_store.create(...)` unchanged — it persists `payload.api_key`/`payload.base_url` (blank is fine; resolution happens at read time via Tasks 6-7). `config=payload.config` already carries `provider_id`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_model_registry_provider_link.py -v`
Expected: PASS.

- [ ] **Step 5: Run the existing registry-route suite (no regression)**

Run: `.venv/bin/python -m pytest tests/unit/test_model_registry_routes.py -v`
Expected: PASS (unchanged — entries without `provider_id` resolve to their own blank/own creds exactly as before).

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/api/routes/model_registry.py tests/unit/test_model_registry_provider_link.py
git -c user.name=lugondev -c user.email=lugondev@gmail.com commit -m "feat(providers): registry test-call resolves provider credentials"
```

---

### Task 6: LLM responder resolves provider credentials at read time

**Files:**
- Modify: `apps/api_gateway/app/services/conversation/responder.py` (`resolve_llm_override_from_registry`, ~line 99, and/or the entry→responder build path)
- Test: `tests/unit/test_responder_provider_creds.py`

**Interfaces:**
- Consumes: `resolve_credentials` (Task 3), existing `_active_llm_entry()` / `resolve_llm_override_from_registry(engine, model)`.
- Behavior: when the active LLM entry has `config.provider_id`, the responder is built with the provider's `(base_url, api_key)` instead of the (possibly blank) entry fields.

- [ ] **Step 1: Read the current resolution path**

Run: `sed -n '95,125p' apps/api_gateway/app/services/conversation/responder.py`
Identify where `entry["base_url"]` / `entry["api_key"]` are read to build the responder (`resolve_llm_override_from_registry` and any `_active_llm_entry()` consumer). This is the single line-pair to route through `resolve_credentials`.

- [ ] **Step 2: Write the failing test**

```python
# tests/unit/test_responder_provider_creds.py
import pytest

from app.services.db.engine import init_db
from app.services.model_registry.store import model_registry_store
from app.services.providers.store import provider_store
from app.services.conversation.responder import resolve_llm_override_from_registry


@pytest.mark.asyncio
async def test_llm_override_uses_provider():
    await init_db()
    prov = await provider_store.create(
        name="qwencloud",
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        api_key="sk-QWEN",
    )
    await model_registry_store.create(
        "llm", "qwencloud", "qwen-max", "Qwen Max",
        api_key="", base_url="", config={"provider_id": prov["id"]}, is_default=True,
    )
    base_url, api_key = await resolve_llm_override_from_registry("qwencloud", "qwen-max")
    assert base_url == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    assert api_key == "sk-QWEN"
```

> If `resolve_llm_override_from_registry`'s real signature/return differs from `(base_url, api_key)` after Step 1, adjust the assertion to its actual shape — but the invariant to assert is "provider creds win over blank entry creds."

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_responder_provider_creds.py -v`
Expected: FAIL — returns entry's blank `("", "")` (or None) instead of provider creds.

- [ ] **Step 4: Route the resolution through `resolve_credentials`**

In `resolve_llm_override_from_registry` (and `_active_llm_entry` consumers that build the responder), after fetching the `entry` dict, replace direct `entry["base_url"]`/`entry["api_key"]` reads with:
```python
from app.services.providers.resolve import resolve_credentials
base_url, api_key = await resolve_credentials(entry)
```
Keep all other behavior (model id, engine) unchanged.

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_responder_provider_creds.py -v`
Expected: PASS.

- [ ] **Step 6: Regression — existing responder/registry tests**

Run: `.venv/bin/python -m pytest tests/unit/test_responder_llm_registry.py -v`
Expected: PASS (entries without provider_id resolve to their own creds).

- [ ] **Step 7: Commit**

```bash
git add apps/api_gateway/app/services/conversation/responder.py tests/unit/test_responder_provider_creds.py
git -c user.name=lugondev -c user.email=lugondev@gmail.com commit -m "feat(providers): LLM responder resolves provider credentials"
```

---

### Task 7: STT/TTS service providers resolve provider credentials

**Files:**
- Modify: `apps/api_gateway/app/services/stt/providers/openrouter_provider.py`, `http_stt_provider.py`
- Modify: `apps/api_gateway/app/services/tts/providers/http_tts_provider.py`
- Test: `tests/unit/test_stt_tts_provider_creds.py`

**Interfaces:**
- Consumes: `resolve_credentials_sync` (Task 3) — these providers build off the event loop, so use the sync variant.
- Behavior: each provider, when constructed from a registry entry whose `config.provider_id` is set, uses the provider's `(base_url, api_key)`; otherwise its own entry fields (unchanged).

- [ ] **Step 1: Read current credential reads**

Run: `grep -n "api_key\|base_url\|entry\[" apps/api_gateway/app/services/stt/providers/openrouter_provider.py apps/api_gateway/app/services/stt/providers/http_stt_provider.py apps/api_gateway/app/services/tts/providers/http_tts_provider.py`
Locate where each reads `entry["api_key"]` / `entry["base_url"]` (or takes them as ctor args from the registry lookup).

- [ ] **Step 2: Write the failing test**

```python
# tests/unit/test_stt_tts_provider_creds.py
import pytest

from app.services.db.engine import init_db
from app.services.providers.store import provider_store
from app.services.providers.resolve import resolve_credentials_sync


@pytest.mark.asyncio
async def test_sync_resolver_used_by_providers():
    await init_db()
    # warm the sync cache
    p = await provider_store.create(name="openai", base_url="https://api.openai.com/v1", api_key="sk-S")
    entry = {"base_url": "", "api_key": "", "config": {"provider_id": p["id"]}}
    assert resolve_credentials_sync(entry) == ("https://api.openai.com/v1", "sk-S")
```

> This asserts the resolver contract the providers now call. Add a focused per-provider assertion only if Step 1 shows a constructor cheap to exercise without network; otherwise this contract test plus the Step 4 wiring is sufficient (no network fixture needed).

- [ ] **Step 3: Run test to verify it fails/passes as expected**

Run: `.venv/bin/python -m pytest tests/unit/test_stt_tts_provider_creds.py -v`
Expected: PASS immediately (resolver already exists) — this test guards the contract the wiring depends on. The behavioral change is verified by Step 5 regression.

- [ ] **Step 4: Wire each provider**

For each of the three provider modules, where it currently reads its endpoint/key from the entry, replace with:
```python
from app.services.providers.resolve import resolve_credentials_sync
base_url, api_key = resolve_credentials_sync(entry)
```
using the resulting `base_url`/`api_key` where the entry fields were used. For `OpenRouterSttProvider` (fixed endpoint, api_key only) resolve only `api_key`:
```python
_, api_key = resolve_credentials_sync(entry)
```
Do not change constructors that already receive an explicit `api_key` from the add-time test-call path (Task 5 supplies resolved creds there) — only the registry-lookup build path needs this.

- [ ] **Step 5: Regression — existing provider tests**

Run: `.venv/bin/python -m pytest tests/unit/test_openrouter_provider.py tests/unit/test_http_stt_provider.py tests/unit/test_http_tts_provider.py -v`
Expected: PASS (entries without provider_id behave exactly as before).

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/services/stt/providers/openrouter_provider.py apps/api_gateway/app/services/stt/providers/http_stt_provider.py apps/api_gateway/app/services/tts/providers/http_tts_provider.py tests/unit/test_stt_tts_provider_creds.py
git -c user.name=lugondev -c user.email=lugondev@gmail.com commit -m "feat(providers): STT/TTS service providers resolve provider credentials"
```

---

### Task 8: Full changed-repo test gate

**Files:** none (verification only).

- [ ] **Step 1: Run the provider + touched suites together**

Run:
```bash
.venv/bin/python -m pytest tests/unit/test_provider_model.py tests/unit/test_provider_store.py \
  tests/unit/test_provider_resolve.py tests/unit/test_providers_routes.py \
  tests/unit/test_model_registry_provider_link.py tests/unit/test_responder_provider_creds.py \
  tests/unit/test_stt_tts_provider_creds.py tests/unit/test_model_registry_routes.py \
  tests/unit/test_responder_llm_registry.py -v
```
Expected: ALL PASS.

- [ ] **Step 2: Pre-push gate — full api_gateway unit suite**

Run: `.venv/bin/python -m pytest tests/unit -q`
Expected: PASS (no regressions). Per repo convention this is the pre-commit/push gate; do not push to main (auto-deploys prod) without the user's go-ahead.

- [ ] **Step 3: Local endpoint smoke (optional, per test-before-deploy memory)**

Boot the app locally and confirm `/v1/providers` responds behind admin auth (see repo run skill). Not a substitute for the test suite; a final sanity check before the user pushes.

---

## Deferred to later plans

- **Plan 2 — Usage & Pricing:** `usage_events` table, `config.price` per model, `compute_cost`, `record_usage`, capture points (LLM `stream_options.include_usage`, STT seconds, TTS chars), thread `user_id`/`profile_id` down to capture points, `/v1/usage/summary` + `/v1/usage/me` (latter in `_AUTH_PREFIXES`).
- **Plan 3 — Quota:** `quotas` + `usage_counters` tables, `quota_gate` pre-flight (user/provider/global), `QuotaExceededError` → block + `status="blocked"` audit row.
- **Gemini native adapter** (non-OpenAI-compat) if ever needed.
- **Seed providers from existing cloud entries** (optional migration convenience).

## Self-Review

- **Spec coverage (§3, §8-§10):** providers table (T1) ✓; store+resolve fallback for local, zero-migration (T2/T3) ✓; provider_id in config not a new column (T1-T3) ✓; CRUD + mask + admin-gate (T4) ✓; presets for the 3 providers (T3/T4) ✓; registry test-call + LLM + STT/TTS read paths all route through resolver (T5/T6/T7) ✓; api_key masking everywhere (T4) ✓; §9 credential storage unchanged (same String(256) as registry) ✓. Usage/quota (§4-§7) intentionally deferred to Plan 2/3.
- **Placeholder scan:** no TBD/TODO; every code step shows real code. Two steps (T6 Step 1, T7 Step 1) require reading current signatures first because those functions' exact shapes weren't fully quoted here — each carries an explicit invariant to assert and a fallback instruction, not a blank.
- **Type consistency:** `resolve_credentials`/`resolve_credentials_sync` return `tuple[str,str]` used identically in T5/T6/T7; `provider_store` dict keys (`id,name,label,base_url,api_key,enabled,config`) consistent across T2-T4; `_mask_api_key` reveal-format matches model_registry's.
