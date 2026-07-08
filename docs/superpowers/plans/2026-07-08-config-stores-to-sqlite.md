# Config Stores → SQLite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Back the four JSON config stores (profiles, tts_profiles, mcp_servers, system_config) with SQLite behind their existing **synchronous** API so the whole app is Postgres-ready via `DATABASE_URL`, with zero call-site changes.

**Architecture:** A sync SQLAlchemy engine (URL derived from `settings.database_url`) + a `SqliteBackedStore` base with an in-memory cache and write-through. Each store keeps its class name, module singleton, and `__init__(path)` signature; `path` becomes the one-time JSON import seed (imported then deleted). Spec: `docs/superpowers/specs/2026-07-08-config-stores-to-sqlite-design.md`.

**Tech Stack:** Python 3.12, SQLAlchemy (sync `create_engine`), Pydantic v2, pytest. Run tests: `.venv/bin/pytest`.

## Global Constraints
- Keep every store's sync API and constructor signature `__init__(self, path: str)`; keep the module singletons `profile_store`, `tts_profile_store`, `mcp_server_store`, `system_config_store`. NO caller becomes async.
- `data` column stores `model.model_dump_json()`; read via `Model.model_validate_json`. Use SQLAlchemy `Text`.
- Config tables use a **separate** declarative Base from the async `app/services/db/models.py::Base`, and a **separate sync engine** — never touch the async engine.
- One DB: sync engine URL is derived from `settings.database_url` by swapping the async driver for the sync one.
- Full existing suite must stay green (currently ~478 passed / 2 skipped / 1 pre-existing unrelated failure `test_conversation_engine_ready`).

---

### Task 1: Sync engine + config tables + test DB isolation

**Files:**
- Create: `apps/api_gateway/app/services/db/sync_engine.py`
- Create: `apps/api_gateway/app/services/db/config_models.py`
- Modify: `tests/conftest.py`
- Test: `tests/unit/test_sync_engine.py`

**Interfaces produced:**
- `sync_database_url(async_url: str) -> str`
- `configure(url: str | None = None) -> None`, `session_scope()` (contextmanager yielding a sync `Session`), `init_config_tables() -> None`
- `config_models.ConfigBase` (DeclarativeBase); tables `ProfileRow`, `TtsProfileRow`, `McpServerRow`, `SystemRow`, each `name`/`id` PK + `data: Mapped[str]` Text.

- [ ] **Step 1: Write `test_sync_engine.py`**
```python
from app.services.db.sync_engine import sync_database_url

def test_sqlite_async_to_sync():
    assert sync_database_url("sqlite+aiosqlite:///data/app.db") == "sqlite:///data/app.db"

def test_postgres_async_to_sync():
    assert sync_database_url("postgresql+asyncpg://u:p@h/db") == "postgresql+psycopg://u:p@h/db"

def test_already_sync_passthrough():
    assert sync_database_url("sqlite:///x.db") == "sqlite:///x.db"
    assert sync_database_url("postgresql+psycopg://u@h/db") == "postgresql+psycopg://u@h/db"
```

- [ ] **Step 2: Run — expect fail** `.venv/bin/pytest tests/unit/test_sync_engine.py -q` → import error.

- [ ] **Step 3: Implement `config_models.py`**
```python
from __future__ import annotations
from sqlalchemy import Integer, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class ConfigBase(DeclarativeBase):
    pass


class ProfileRow(ConfigBase):
    __tablename__ = "config_profiles"
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    data: Mapped[str] = mapped_column(Text)


class TtsProfileRow(ConfigBase):
    __tablename__ = "config_tts_profiles"
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    data: Mapped[str] = mapped_column(Text)


class McpServerRow(ConfigBase):
    __tablename__ = "config_mcp_servers"
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    data: Mapped[str] = mapped_column(Text)


class SystemRow(ConfigBase):
    __tablename__ = "config_system"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    data: Mapped[str] = mapped_column(Text)
```

- [ ] **Step 4: Implement `sync_engine.py`**
```python
"""Synchronous SQLAlchemy engine for the config stores.

Same DB as the async engine (sessions/memories), reached with the sync driver so
the stores' synchronous API needs no await. URL derived from settings.database_url.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.settings import settings

_engine = None
_factory: sessionmaker | None = None
_tables_ready = False


def sync_database_url(async_url: str) -> str:
    if async_url.startswith("sqlite+aiosqlite"):
        return async_url.replace("sqlite+aiosqlite", "sqlite", 1)
    if async_url.startswith("postgresql+asyncpg"):
        return async_url.replace("postgresql+asyncpg", "postgresql+psycopg", 1)
    return async_url  # already sync (or an unknown scheme — pass through)


def configure(url: str | None = None) -> None:
    global _engine, _factory, _tables_ready
    if _engine is not None:
        _engine.dispose()
    sync_url = sync_database_url(url or settings.database_url)
    if sync_url.startswith("sqlite"):
        db_file = sync_url.split("///", 1)[-1]
        if db_file and db_file != ":memory:":
            Path(db_file).parent.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(sync_url, future=True)
    _factory = sessionmaker(_engine, expire_on_commit=False)
    _tables_ready = False


def init_config_tables() -> None:
    global _tables_ready
    if _factory is None:
        configure()
    if _tables_ready:
        return
    from app.services.db.config_models import ConfigBase
    ConfigBase.metadata.create_all(_engine)
    _tables_ready = True


@contextmanager
def session_scope():
    if _factory is None:
        configure()
    s: Session = _factory()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
```

- [ ] **Step 5: Extend `tests/conftest.py` `_tmp_db` fixture** to isolate the sync engine too. In the `_tmp_db` fixture, after configuring the async engine, add:
```python
    from app.services.db import sync_engine as cfg_engine
    cfg_engine.configure(f"sqlite:///{tmp_path}/test.db")
    yield
    db_engine.configure()
    cfg_engine.configure()
```
(Replace the existing single `yield`/teardown so both engines reset. Keep the async `db_engine.configure(...)` line.)

- [ ] **Step 6: Run** `.venv/bin/pytest tests/unit/test_sync_engine.py -q` → PASS.

- [ ] **Step 7: Commit**
```bash
git add apps/api_gateway/app/services/db/sync_engine.py apps/api_gateway/app/services/db/config_models.py tests/conftest.py tests/unit/test_sync_engine.py
git commit -m "feat(db): sync SQLAlchemy engine + config tables for the config stores"
```

---

### Task 2: `SqliteBackedStore` base + migrate ProfileStore

**Files:**
- Create: `apps/api_gateway/app/services/db/config_store.py`
- Modify: `apps/api_gateway/app/services/profiles/store.py`
- Test: `tests/unit/test_config_store.py`

**Interfaces:**
- Consumes Task 1's `session_scope`, `init_config_tables`, `ProfileRow`.
- Produces `SqliteBackedStore` (generic keyed store) and the rebuilt `ProfileStore(path)` + `profile_store` singleton with the SAME `list/get/upsert/delete` API.

- [ ] **Step 1: Write `test_config_store.py`** (exercises the base via ProfileStore; the `_tmp_db` fixture gives a fresh per-test DB)
```python
import json
from pathlib import Path
from app.services.profiles.models import Profile
from app.services.profiles.store import ProfileStore

def test_crud_roundtrip_persists(tmp_path):
    s = ProfileStore(str(tmp_path / "profiles.json"))
    s.upsert(Profile(name="a"))
    assert "a" in s.list()
    assert s.get("a").name == "a"
    # a fresh instance sees it (persisted to the DB, not just cache)
    assert ProfileStore(str(tmp_path / "profiles.json")).get("a") is not None
    s.delete("a")
    assert s.get("a") is None

def test_imports_legacy_json_then_deletes_file(tmp_path):
    p = tmp_path / "profiles.json"
    p.write_text(json.dumps({"profiles": {"seed": Profile(name="seed").model_dump()}}))
    s = ProfileStore(str(p))
    assert s.get("seed").name == "seed"   # imported into the DB
    assert not p.exists()                  # legacy file removed after import

def test_no_reimport_when_table_has_rows(tmp_path):
    p = tmp_path / "profiles.json"
    ProfileStore(str(p)).upsert(Profile(name="live"))   # table now has a row; p never created
    p.write_text(json.dumps({"profiles": {"stale": Profile(name="stale").model_dump()}}))
    s = ProfileStore(str(p))
    assert s.get("live") is not None
    assert s.get("stale") is None          # not imported (table already had data)
    assert p.exists()                      # left alone (no import happened)
```

- [ ] **Step 2: Run — expect fail** (new ProfileStore behavior not implemented).

- [ ] **Step 3: Implement `config_store.py`**
```python
from __future__ import annotations

import os
import threading
from typing import Callable, Generic, TypeVar

from pydantic import BaseModel
from sqlalchemy import delete as sa_delete, select

from app.services.db.sync_engine import init_config_tables, session_scope

M = TypeVar("M", bound=BaseModel)


class SqliteBackedStore(Generic[M]):
    """Keyed config store: in-memory cache + write-through to a (name, data) table.

    Subclass/parameterize with the SQLAlchemy row class, the Pydantic model, the
    model's key attribute, and a callable that parses a legacy-JSON dict of
    {name: model_dict} for the one-time import.
    """

    def __init__(
        self,
        path: str,
        *,
        row_cls: type,
        model_cls: type[M],
        key_attr: str,
        legacy_parse: Callable[[str], dict[str, M]],
    ) -> None:
        self._path = path
        self._row = row_cls
        self._model = model_cls
        self._key = key_attr
        self._legacy_parse = legacy_parse
        self._lock = threading.Lock()
        self._cache: dict[str, M] | None = None

    def _ensure(self) -> None:
        if self._cache is not None:
            return
        init_config_tables()
        with session_scope() as s:
            rows = s.execute(select(self._row)).scalars().all()
            self._cache = {r.name: self._model.model_validate_json(r.data) for r in rows}
        if not self._cache and self._path and os.path.exists(self._path):
            self._import_legacy()

    def _import_legacy(self) -> None:
        try:
            seed = self._legacy_parse(self._path)
        except Exception:
            seed = {}
        for model in seed.values():
            self._put(model)
        try:
            os.remove(self._path)
        except OSError:
            pass

    def _put(self, model: M) -> None:
        name = getattr(model, self._key)
        with session_scope() as s:
            row = s.get(self._row, name)
            if row is None:
                s.add(self._row(name=name, data=model.model_dump_json()))
            else:
                row.data = model.model_dump_json()
        self._cache[name] = model

    def list(self) -> dict[str, M]:
        with self._lock:
            self._ensure()
            return dict(self._cache)

    def get(self, name: str) -> M | None:
        with self._lock:
            self._ensure()
            return self._cache.get(name)

    def upsert(self, model: M) -> None:
        with self._lock:
            self._ensure()
            self._put(model)

    def delete(self, name: str) -> None:
        with self._lock:
            self._ensure()
            with session_scope() as s:
                s.execute(sa_delete(self._row).where(self._row.name == name))
            self._cache.pop(name, None)
```

- [ ] **Step 4: Rebuild `profiles/store.py`**
```python
from __future__ import annotations

import json

from app.core.settings import settings
from app.services.db.config_models import ProfileRow
from app.services.db.config_store import SqliteBackedStore
from app.services.profiles.models import Profile


def _parse_legacy(path: str) -> dict[str, Profile]:
    data = json.loads(open(path).read()).get("profiles", {})
    return {k: Profile.model_validate(v) for k, v in data.items()}


class ProfileStore(SqliteBackedStore[Profile]):
    def __init__(self, path: str) -> None:
        super().__init__(
            path, row_cls=ProfileRow, model_cls=Profile,
            key_attr="name", legacy_parse=_parse_legacy,
        )


profile_store = ProfileStore(settings.profiles_path)
```

- [ ] **Step 5: Run** `.venv/bin/pytest tests/unit/test_config_store.py -q` → PASS.

- [ ] **Step 6: Regression — profiles routes + lugo (they read profile_store)**
Run: `.venv/bin/pytest tests/unit -k "profile or lugo" -q`
Expected: PASS. If a test constructed `ProfileStore(tmp)` and expected file isolation, it now gets DB isolation via `_tmp_db` — should still pass; if any references `profiles.json` on disk directly, note it in the report.

- [ ] **Step 7: Commit**
```bash
git add apps/api_gateway/app/services/db/config_store.py apps/api_gateway/app/services/profiles/store.py tests/unit/test_config_store.py
git commit -m "feat(profiles): back ProfileStore with SQLite (cache + write-through), import+drop JSON"
```

---

### Task 3: Migrate TtsProfileStore + McpServerStore

**Files:**
- Modify: `apps/api_gateway/app/services/tts/profile_store.py`, `apps/api_gateway/app/services/mcp/<mcp store file>.py` (find with `grep -rl "class McpServerStore" apps`)
- Test: `tests/unit/test_config_store.py` (append)

**Interfaces:** same pattern as ProfileStore, using `TtsProfileRow` / `McpServerRow` and the respective models (`TtsProfile`, `McpServer`). Preserve legacy JSON shapes: inspect each current store's `_read()` to replicate the exact JSON structure in `_parse_legacy` (ProfileStore/McpServerStore wrap under a top key like `{"profiles": …}` / `{"servers": …}`; confirm the tts store's shape before writing its parser).

- [ ] **Step 1: Append tests** mirroring Task 2's three tests for `TtsProfileStore` and `McpServerStore` (crud+persist, import+delete, no-reimport). Use each model's minimal constructor (check required fields).

- [ ] **Step 2: Run — expect fail.**

- [ ] **Step 3: Rebuild both stores** as `SqliteBackedStore` subclasses (mirror the ProfileStore rewrite), each with its own `_parse_legacy` matching the current on-disk shape, keeping the singleton names `tts_profile_store` / `mcp_server_store` and `__init__(path)`.

- [ ] **Step 4: Run** `.venv/bin/pytest tests/unit/test_config_store.py -q` → PASS.

- [ ] **Step 5: Regression** `.venv/bin/pytest tests/unit -k "tts or mcp" -q` → PASS.

- [ ] **Step 6: Commit**
```bash
git add apps/api_gateway/app/services/tts/profile_store.py apps/api_gateway/app/services/mcp tests/unit/test_config_store.py
git commit -m "feat(tts,mcp): back TtsProfileStore + McpServerStore with SQLite"
```

---

### Task 4: Migrate SystemConfigStore (singleton)

**Files:**
- Modify: `apps/api_gateway/app/services/system_config.py`
- Test: `tests/unit/test_system_config_store.py`

**Interfaces:** keep `SystemConfigStore(path)`, `system_config_store`, methods `get() -> SystemConfig` and `set_base_context(value) -> SystemConfig`. One row `id=1` in `config_system`.

- [ ] **Step 1: Write test**
```python
from app.services.system_config import SystemConfigStore

def test_default_when_empty(tmp_path):
    s = SystemConfigStore(str(tmp_path / "system_config.json"))
    assert s.get().base_context == ""

def test_set_persists_across_instances(tmp_path):
    p = str(tmp_path / "system_config.json")
    SystemConfigStore(p).set_base_context("hello")
    assert SystemConfigStore(p).get().base_context == "hello"

def test_imports_legacy_then_deletes(tmp_path):
    from app.services.system_config import SystemConfig
    p = tmp_path / "system_config.json"
    p.write_text(SystemConfig(base_context="seeded").model_dump_json())
    s = SystemConfigStore(str(p))
    assert s.get().base_context == "seeded"
    assert not p.exists()
```

- [ ] **Step 2: Run — expect fail.**

- [ ] **Step 3: Implement** — rewrite `SystemConfigStore` to read/write the single `SystemRow(id=1)` via `session_scope`, with an in-memory cache, importing the legacy JSON (`SystemConfig.model_validate_json(file)`) on first access if the row is missing, then deleting the file. `set_base_context` writes `SystemConfig(base_context=value)` to the row + cache.

- [ ] **Step 4: Run** `.venv/bin/pytest tests/unit/test_system_config_store.py -q` → PASS.

- [ ] **Step 5: Regression** `.venv/bin/pytest tests/unit -k "system_config or base_context" -q` → PASS.

- [ ] **Step 6: Commit**
```bash
git add apps/api_gateway/app/services/system_config.py tests/unit/test_system_config_store.py
git commit -m "feat(system-config): back SystemConfigStore with SQLite (single-row)"
```

---

### Task 5: Startup wiring + Postgres dependency + full regression

**Files:**
- Modify: `apps/api_gateway/app/main.py` (lifespan)
- Modify: `pyproject.toml`

- [ ] **Step 1: Eager-load stores at startup.** In `main.py` `lifespan`, after `await init_db()`, add a call that loads all four caches (so the first request doesn't pay the import/scan and the JSON import happens at boot):
```python
    from app.services.db.sync_engine import init_config_tables
    init_config_tables()
    from app.services.profiles.store import profile_store
    from app.services.tts.profile_store import tts_profile_store
    from app.services.system_config import system_config_store
    profile_store.list(); tts_profile_store.list(); system_config_store.get()
    # McpServerStore too — import its singleton and call list()
```
(Use the actual mcp singleton import path.) Each `list()/get()` triggers `_ensure()` → table create + legacy import + cache fill.

- [ ] **Step 2: Add the sync Postgres driver** to `pyproject.toml` dependencies: `psycopg[binary]` (alongside the existing async `asyncpg` if present; if the project has no postgres deps yet, add both `asyncpg` and `psycopg[binary]`). SQLite needs none. Do NOT run a full `uv sync` if it would churn the lockfile unnecessarily — just add the dependency line; note in the report that install is needed only when switching to Postgres.

- [ ] **Step 3: Full regression gate**
Run: `.venv/bin/pytest -q`
Expected: all green except the known pre-existing `test_conversation_engine_ready::test_session_started_reports_ready_when_already_warm`. Fix anything this migration broke.

- [ ] **Step 4: Sanity — no stale JSON reads remain.** `grep -rn "profiles.json\|tts_profiles.json\|mcp_servers.json\|system_config.json\|_read()\|_write(" apps/api_gateway/app/services/profiles apps/api_gateway/app/services/tts apps/api_gateway/app/services/mcp apps/api_gateway/app/services/system_config.py` — confirm no leftover file-based read/write paths in the rebuilt stores.

- [ ] **Step 5: Commit**
```bash
git add apps/api_gateway/app/main.py pyproject.toml
git commit -m "feat(config): load config stores at startup; add sync psycopg for Postgres"
```

## Self-review notes
- API unchanged (sync `list/get/upsert/delete`, `__init__(path)`, singleton names) → no caller ripple; the ~30 call sites and the routes are covered by the existing suite (regression gate in Tasks 2/3/4/5).
- Test isolation via the extended `_tmp_db` fixture (per-test sync DB) — mirrors the async engine's existing isolation.
- Import-once-then-delete gives a clean cutover; `no_reimport` test guards against re-importing a stale file after real data exists.
- Postgres: `sync_database_url` derivation + `psycopg` dep; swapping `DATABASE_URL` moves both engines.
