# Memory User Scoping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Key chat memory by `(user_id, profile_id)` instead of `profile_id` alone, so users on a shared template profile no longer contaminate each other's memories and memories survive a persona switch.

**Architecture:** `user_id` is threaded from the caller (WS `SessionConfig.identity_user_id`, HTTP `current_user_id(request)`) down through extractor → store → compactor → retriever. Absent identity normalizes to `''` (the shared-`DEVICE_AUTH_TOKEN` fleet bucket). `MemoryProfileDoc` gains a composite primary key `(user_id, profile_id)`; the schema change rides the existing `init_db()` DDL path (`_ensure_column` siblings), not a `main.py` data migration. `memories` needs no DDL — its `user_id` column already exists — only a NULL→`''` backfill and per-query filtering.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async (aiosqlite), pytest (`asyncio_mode=auto`), `.venv/bin/python`.

## Global Constraints

- Run tests with `.venv/bin/python -m pytest` (Python 3.12). NOT the pyenv 3.14 interpreter — it lacks pytest-timeout and ML wheels.
- `''` (empty string), never `None`, is the stored sentinel for "no attributable user". Postgres forbids NULL in a primary key; `''` keeps the composite PK portable.
- Normalize `user_id` at the store boundary: `_uid(user_id) -> user_id or ""`. Every store method that takes `user_id` applies it, so callers may pass `None` or `''` interchangeably.
- Best-effort invariant is load-bearing: every memory path stays wrapped so a failure logs and swallows, never breaking a session teardown or a reply. Do not remove existing `except` guards.
- Always 404 (never 403) from `routes/memories.py`, mirroring `profiles.py`, so probing cannot enumerate other users' profile names.
- Reuse `profiles.py`'s `_visible` / `_can_write` helpers; do not reimplement ownership logic.

---

### Task 1: Store layer — scope every read/write by `user_id`; composite doc PK

**Files:**
- Modify: `apps/api_gateway/app/services/db/models.py:56-64` (MemoryProfileDoc composite PK)
- Modify: `apps/api_gateway/app/services/memory/store.py` (all methods)
- Test: `tests/unit/test_memory_store.py`, `tests/unit/test_memory_profile_doc_store.py`

**Interfaces:**
- Produces:
  - `_uid(user_id: str | None) -> str`
  - `MemoryStore.list(profile_id: str, user_id: str | None = None) -> list[dict]`
  - `MemoryStore.add(profile_id: str, content: str, *, source_session_id: str | None = None, embedding: list[float] | None = None, user_id: str | None = None) -> dict`
  - `MemoryStore.update(memory_id: str, content: str, *, profile_id: str | None = None, user_id: str | None = None) -> dict | None`
  - `MemoryStore.delete(memory_id: str, *, profile_id: str | None = None, user_id: str | None = None) -> bool`
  - `MemoryStore.delete_all(profile_id: str, user_id: str | None = None) -> int`
  - `MemoryStore.delete_many(ids: list[str]) -> int` (unchanged)
  - `ProfileDocStore.get(profile_id: str, user_id: str | None = None) -> dict | None`
  - `ProfileDocStore.upsert(profile_id: str, content: str, user_id: str | None = None) -> dict`
  - `ProfileDocStore.delete(profile_id: str, user_id: str | None = None) -> bool`
- `_doc_dict` gains a `"user_id"` key.

- [ ] **Step 1: Write failing tests for user-scoped MemoryStore**

Append to `tests/unit/test_memory_store.py`:

```python
async def test_list_scopes_by_user():
    from app.services.memory.store import memory_store

    await memory_store.add("shared", "a-fact", user_id="user-a")
    await memory_store.add("shared", "b-fact", user_id="user-b")

    a = await memory_store.list("shared", user_id="user-a")
    assert [m["content"] for m in a] == ["a-fact"]
    assert a[0]["user_id"] == "user-a"

    b = await memory_store.list("shared", user_id="user-b")
    assert [m["content"] for m in b] == ["b-fact"]


async def test_none_user_normalizes_to_empty_string_bucket():
    from app.services.memory.store import memory_store

    await memory_store.add("dev", "device-fact", user_id=None)
    rows = await memory_store.list("dev", user_id="")
    assert [m["content"] for m in rows] == ["device-fact"]
    assert rows[0]["user_id"] == ""


async def test_delete_all_scopes_by_user():
    from app.services.memory.store import memory_store

    await memory_store.add("shared", "a-fact", user_id="user-a")
    await memory_store.add("shared", "b-fact", user_id="user-b")

    deleted = await memory_store.delete_all("shared", user_id="user-a")
    assert deleted == 1
    assert [m["content"] for m in await memory_store.list("shared", user_id="user-b")] == ["b-fact"]


async def test_update_and_delete_reject_wrong_user():
    from app.services.memory.store import memory_store

    row = await memory_store.add("shared", "a-fact", user_id="user-a")
    mid = row["id"]

    assert await memory_store.update(mid, "hax", profile_id="shared", user_id="user-b") is None
    assert await memory_store.delete(mid, profile_id="shared", user_id="user-b") is False
    # owner still can
    assert (await memory_store.update(mid, "fixed", profile_id="shared", user_id="user-a"))["content"] == "fixed"
    assert await memory_store.delete(mid, profile_id="shared", user_id="user-a") is True
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/unit/test_memory_store.py -q`
Expected: FAIL — `list()` takes no `user_id`, results not scoped.

- [ ] **Step 3: Implement user scoping in `store.py`**

Add near the top of `store.py` (after imports):

```python
def _uid(user_id: str | None) -> str:
    return user_id or ""
```

Add `"user_id": m.user_id,` is already present in `_mem_dict`; leave it. Replace the `MemoryStore` methods:

```python
class MemoryStore:
    async def list(self, profile_id: str, user_id: str | None = None) -> list[dict]:
        async with db_session() as s:
            rows = (
                await s.execute(
                    select(MemoryItem)
                    .where(
                        MemoryItem.profile_id == profile_id,
                        MemoryItem.user_id == _uid(user_id),
                    )
                    .order_by(MemoryItem.created_at.desc(), MemoryItem.id)
                )
            ).scalars().all()
            return [_mem_dict(m) for m in rows]

    async def add(
        self,
        profile_id: str,
        content: str,
        *,
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
                user_id=_uid(user_id),
            )
            s.add(row)
            await s.commit()
            return _mem_dict(row)

    async def update(
        self, memory_id: str, content: str, *,
        profile_id: str | None = None, user_id: str | None = None,
    ) -> dict | None:
        async with db_session() as s:
            row = await s.get(MemoryItem, memory_id)
            if not row:
                return None
            if profile_id is not None and row.profile_id != profile_id:
                return None
            if user_id is not None and row.user_id != _uid(user_id):
                return None
            row.content = content
            row.updated_at = utcnow()
            await s.commit()
            return _mem_dict(row)

    async def delete(
        self, memory_id: str, *,
        profile_id: str | None = None, user_id: str | None = None,
    ) -> bool:
        async with db_session() as s:
            row = await s.get(MemoryItem, memory_id)
            if not row:
                return False
            if profile_id is not None and row.profile_id != profile_id:
                return False
            if user_id is not None and row.user_id != _uid(user_id):
                return False
            await s.delete(row)
            await s.commit()
            return True

    async def delete_all(self, profile_id: str, user_id: str | None = None) -> int:
        async with db_session() as s:
            result = await s.execute(
                sa_delete(MemoryItem).where(
                    MemoryItem.profile_id == profile_id,
                    MemoryItem.user_id == _uid(user_id),
                )
            )
            await s.commit()
            return result.rowcount or 0

    async def delete_many(self, ids: list[str]) -> int:
        if not ids:
            return 0
        async with db_session() as s:
            result = await s.execute(sa_delete(MemoryItem).where(MemoryItem.id.in_(ids)))
            await s.commit()
            return result.rowcount or 0
```

Note: `update`/`delete` become keyword-only for `profile_id`/`user_id`. Grep for existing callers before finishing this task: `grep -rn "memory_store.update\|memory_store.delete(" apps tests` — the only production callers are `routes/memories.py` (Task 7) and `compactor.delete_many` (unaffected). Existing `test_memories_routes.py` calls go through the route, not the store directly.

- [ ] **Step 4: Change `MemoryProfileDoc` to a composite primary key**

In `models.py`, replace the `MemoryProfileDoc` class body:

```python
class MemoryProfileDoc(Base):
    __tablename__ = "memory_profile_docs"

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True, default="")
    profile_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    content: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
```

- [ ] **Step 5: Write failing tests for user-scoped ProfileDocStore**

Append to `tests/unit/test_memory_profile_doc_store.py`:

```python
async def test_doc_is_scoped_by_user():
    from app.services.memory.store import profile_doc_store

    await profile_doc_store.upsert("shared", "A's profile", user_id="user-a")
    await profile_doc_store.upsert("shared", "B's profile", user_id="user-b")

    a = await profile_doc_store.get("shared", user_id="user-a")
    b = await profile_doc_store.get("shared", user_id="user-b")
    assert a["content"] == "A's profile"
    assert a["user_id"] == "user-a"
    assert b["content"] == "B's profile"


async def test_doc_none_user_uses_empty_bucket():
    from app.services.memory.store import profile_doc_store

    await profile_doc_store.upsert("dev", "device doc", user_id=None)
    got = await profile_doc_store.get("dev", user_id="")
    assert got["content"] == "device doc"
    assert await profile_doc_store.delete("dev", user_id="") is True
    assert await profile_doc_store.get("dev", user_id="") is None
```

- [ ] **Step 6: Implement user scoping in `ProfileDocStore`**

Replace `_doc_dict` and `ProfileDocStore`:

```python
def _doc_dict(d: MemoryProfileDoc) -> dict:
    return {
        "profile_id": d.profile_id,
        "user_id": d.user_id,
        "content": d.content,
        "updated_at": iso_utc(d.updated_at),
    }


class ProfileDocStore:
    async def get(self, profile_id: str, user_id: str | None = None) -> dict | None:
        async with db_session() as s:
            row = await s.get(MemoryProfileDoc, (_uid(user_id), profile_id))
            return _doc_dict(row) if row else None

    async def upsert(self, profile_id: str, content: str, user_id: str | None = None) -> dict:
        async with db_session() as s:
            row = await s.get(MemoryProfileDoc, (_uid(user_id), profile_id))
            if row is None:
                row = MemoryProfileDoc(profile_id=profile_id, content=content, user_id=_uid(user_id))
                s.add(row)
            else:
                row.content = content
                row.updated_at = utcnow()
            await s.commit()
            return _doc_dict(row)

    async def delete(self, profile_id: str, user_id: str | None = None) -> bool:
        async with db_session() as s:
            row = await s.get(MemoryProfileDoc, (_uid(user_id), profile_id))
            if row is None:
                return False
            await s.delete(row)
            await s.commit()
            return True
```

`s.get(Model, (user_id, profile_id))` passes the composite key in declared PK column order (`user_id` first).

- [ ] **Step 7: Run store tests to verify pass**

Run: `.venv/bin/python -m pytest tests/unit/test_memory_store.py tests/unit/test_memory_profile_doc_store.py -q`
Expected: PASS (existing + new).

- [ ] **Step 8: Commit**

```bash
git add apps/api_gateway/app/services/db/models.py apps/api_gateway/app/services/memory/store.py tests/unit/test_memory_store.py tests/unit/test_memory_profile_doc_store.py
git commit -m "feat(memory): scope store reads/writes by user_id; composite doc PK"
```

---

### Task 2: Schema migration in `init_db` — backfill `memories`, rebuild doc PK

**Files:**
- Modify: `apps/api_gateway/app/services/db/engine.py:86-105` (`init_db`, new helpers)
- Test: `tests/unit/test_memory_schema_migration.py` (create)

**Interfaces:**
- Consumes: composite-PK `MemoryProfileDoc` model from Task 1.
- Produces: idempotent startup migration — existing DBs get `memories.user_id` NULL→`''` and `memory_profile_docs` rebuilt under composite PK with every legacy row mapped to `user_id=''`.

**Why here:** `init_db` already hosts this codebase's schema-DDL migrations via `_ensure_column` (`engine.py:99-101`), which use SQLite `PRAGMA` directly. The five `migrate_*` in `main.py` are *data* migrations (config→registry), a different category. Schema DDL belongs next to `_ensure_column`. This corrects the spec's "sixth migration in main.py" framing.

- [ ] **Step 1: Write the failing migration test**

Create `tests/unit/test_memory_schema_migration.py`:

```python
"""A DB created under the OLD single-PK doc schema must, after init_db,
carry every legacy doc under user_id='' and accept per-user docs on one
profile without a PK collision."""

import pytest
from sqlalchemy import text

from app.services.db import engine as db_engine


@pytest.fixture
async def old_schema_db(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 'old.db'}"
    db_engine.configure(url)
    # Simulate a pre-migration DB: single-PK doc table + a NULL-user memory row.
    eng = db_engine.get_engine()
    async with eng.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE memory_profile_docs ("
            "profile_id VARCHAR(128) PRIMARY KEY, content TEXT, updated_at DATETIME)"
        )
        await conn.exec_driver_sql(
            "INSERT INTO memory_profile_docs (profile_id, content, updated_at) "
            "VALUES ('legacy', 'old doc', '2026-01-01 00:00:00')"
        )
        await conn.exec_driver_sql(
            "CREATE TABLE memories (id VARCHAR(36) PRIMARY KEY, profile_id VARCHAR(128), "
            "content TEXT, source_session_id VARCHAR(36), embedding JSON, "
            "created_at DATETIME, updated_at DATETIME)"
        )
        await conn.exec_driver_sql(
            "INSERT INTO memories (id, profile_id, content, created_at, updated_at) "
            "VALUES ('m1', 'legacy', 'device fact', '2026-01-01', '2026-01-01')"
        )
    # Reset the init guard so init_db re-runs against this DB.
    db_engine._initialized = False
    yield url
    db_engine._initialized = False


async def test_migration_backfills_and_rebuilds(old_schema_db):
    from app.services.memory.store import memory_store, profile_doc_store

    await db_engine.init_db()

    # memories NULL user backfilled to ''
    assert [m["content"] for m in await memory_store.list("legacy", user_id="")] == ["device fact"]

    # legacy doc preserved under ''
    assert (await profile_doc_store.get("legacy", user_id=""))["content"] == "old doc"

    # composite PK now allows two users on one profile
    await profile_doc_store.upsert("legacy", "A doc", user_id="user-a")
    await profile_doc_store.upsert("legacy", "B doc", user_id="user-b")
    assert (await profile_doc_store.get("legacy", user_id="user-a"))["content"] == "A doc"
    assert (await profile_doc_store.get("legacy", user_id="user-b"))["content"] == "B doc"


async def test_migration_is_idempotent(old_schema_db):
    await db_engine.init_db()
    db_engine._initialized = False
    await db_engine.init_db()  # second run must not raise or duplicate
    from app.services.memory.store import profile_doc_store
    assert (await profile_doc_store.get("legacy", user_id=""))["content"] == "old doc"
```

If `db_engine.get_engine()` does not exist, add a thin accessor in `engine.py`:

```python
def get_engine() -> AsyncEngine:
    if _engine is None:
        configure()
    assert _engine is not None
    return _engine
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/unit/test_memory_schema_migration.py -q`
Expected: FAIL — second `upsert` on `'legacy'` with a different user hits the old single-column PK and raises IntegrityError (or the backfill assertion fails).

- [ ] **Step 3: Implement the migration helpers in `engine.py`**

Add after `_ensure_column`:

```python
async def _backfill_null_user_ids(conn, table: str) -> None:
    """NULL user_id -> '' so rows land in the shared-device bucket and match
    the composite-key filters. Idempotent."""
    await conn.exec_driver_sql(f"UPDATE {table} SET user_id = '' WHERE user_id IS NULL")


async def _ensure_doc_composite_pk(conn) -> None:
    """Rebuild memory_profile_docs under PK (user_id, profile_id) if it still
    has the legacy single-column PK. SQLite cannot ALTER a primary key, so
    rename-copy-drop. Idempotent: no-op once user_id is already part of the PK."""
    info = await conn.exec_driver_sql("PRAGMA table_info(memory_profile_docs)")
    rows = info.fetchall()
    if not rows:
        return  # table absent; create_all already made it with the new PK
    pk_cols = {r[1] for r in rows if r[5]}  # r[5] = pk position, nonzero => PK member
    if "user_id" in pk_cols:
        return  # already migrated
    await conn.exec_driver_sql("ALTER TABLE memory_profile_docs RENAME TO _mpd_old")
    await conn.exec_driver_sql(
        "CREATE TABLE memory_profile_docs ("
        "user_id VARCHAR(36) NOT NULL DEFAULT '', "
        "profile_id VARCHAR(128) NOT NULL, "
        "content TEXT DEFAULT '', "
        "updated_at DATETIME, "
        "PRIMARY KEY (user_id, profile_id))"
    )
    await conn.exec_driver_sql(
        "INSERT INTO memory_profile_docs (user_id, profile_id, content, updated_at) "
        "SELECT COALESCE(user_id, ''), profile_id, content, updated_at FROM _mpd_old"
    )
    await conn.exec_driver_sql("DROP TABLE _mpd_old")
```

Then wire them into `init_db`, immediately after the `_ensure_column` block (still inside `_engine.begin()`):

```python
            await _ensure_column(conn, "model_registry_entries", "config", "JSON DEFAULT '{}'")
            await _backfill_null_user_ids(conn, "memories")
            await _ensure_doc_composite_pk(conn)
```

The `_ensure_column(conn, "memory_profile_docs", "user_id", ...)` line above already guarantees `_mpd_old` has a `user_id` column to copy from.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/unit/test_memory_schema_migration.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/db/engine.py tests/unit/test_memory_schema_migration.py
git commit -m "feat(memory): backfill user_id and rebuild doc PK on startup"
```

---

### Task 3: Extractor — attribute facts to the acting user

**Files:**
- Modify: `apps/api_gateway/app/services/memory/extractor.py:111-155`
- Test: `tests/unit/test_memory_extractor.py`

**Interfaces:**
- Consumes: user-scoped `memory_store` (Task 1).
- Produces: `MemoryExtractor.extract_and_upsert(session_id: str, profile: Profile, user_id: str | None = None) -> int`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_memory_extractor.py` (follow the file's existing fixture/mocking style for `extract`):

```python
async def test_extract_attributes_to_passed_user_not_profile_owner(monkeypatch):
    from app.services.memory import extractor as ex
    from app.services.memory.store import memory_store
    from app.services.profiles.models import LlmConfig, MemoryConfig, Profile

    profile = Profile(
        name="template",
        owner_id=None,  # a template: the old code would store user_id=None
        llm=LlmConfig(base_url="http://x", model="m"),
        memory=MemoryConfig(enabled=True),
    )
    monkeypatch.setattr(ex.session_store, "get_messages",
                        _fake_messages([{"role": "user", "content": "hi"},
                                        {"role": "assistant", "content": "yo"}]))
    monkeypatch.setattr(ex.MemoryExtractor, "extract",
                        _fake_extract(["User likes tea"]))

    added = await ex.memory_extractor.extract_and_upsert("s1", profile, user_id="user-a")
    assert added == 1
    assert [m["content"] for m in await memory_store.list("template", user_id="user-a")] == ["User likes tea"]
    # NOT under the None/'' bucket
    assert await memory_store.list("template", user_id="") == []
```

Add the two helpers at the top of the file if not already present (mirror the module's existing async-stub pattern):

```python
def _fake_messages(msgs):
    async def _get(_sid):
        return msgs
    return _get


def _fake_extract(facts):
    async def _ex(self, *a, **k):
        return facts
    return _ex
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/unit/test_memory_extractor.py -q`
Expected: FAIL — `extract_and_upsert` takes no `user_id`; facts stored under `profile.owner_id` (None→'').

- [ ] **Step 3: Implement**

Change the signature and the two scoped calls in `extract_and_upsert`:

```python
    async def extract_and_upsert(
        self, session_id: str, profile: Profile, user_id: str | None = None
    ) -> int:
```

Replace `existing_items = await memory_store.list(profile.name)` with:

```python
            existing_items = await memory_store.list(profile.name, user_id=user_id)
```

Replace the `memory_store.add(...)` call with:

```python
                await memory_store.add(
                    profile.name, fact, source_session_id=session_id, embedding=vec,
                    user_id=user_id,
                )
```

Replace `await memory_compactor.maybe_compact(profile)` with:

```python
            await memory_compactor.maybe_compact(profile, user_id=user_id)
```

(`maybe_compact`'s new `user_id` param lands in Task 4; this line will not run until then, and the extractor tests here inject facts below the compaction threshold so `maybe_compact` returns early regardless.)

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/unit/test_memory_extractor.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/memory/extractor.py tests/unit/test_memory_extractor.py
git commit -m "feat(memory): attribute extracted facts to the acting user"
```

---

### Task 4: Compactor — key the profile doc by user

**Files:**
- Modify: `apps/api_gateway/app/services/memory/compactor.py:72-109`
- Test: `tests/unit/test_memory_compactor.py`

**Interfaces:**
- Consumes: user-scoped `memory_store`, `profile_doc_store` (Task 1).
- Produces:
  - `MemoryCompactor.maybe_compact(profile: Profile, user_id: str | None = None) -> bool`
  - `MemoryCompactor.compact(profile: Profile, user_id: str | None = None, items: list[dict] | None = None) -> bool`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_memory_compactor.py` (mirror its existing LLM-mock style):

```python
async def test_compaction_reads_and_writes_the_user_bucket(monkeypatch):
    from app.services.memory import compactor as comp
    from app.services.memory.store import memory_store, profile_doc_store
    from app.services.profiles.models import LlmConfig, MemoryConfig, Profile

    profile = Profile(
        name="template", owner_id=None,
        llm=LlmConfig(base_url="http://x", model="m"),
        memory=MemoryConfig(enabled=True, compaction_threshold=2),
    )
    await memory_store.add("template", "f1", user_id="user-a")
    await memory_store.add("template", "f2", user_id="user-a")
    await memory_store.add("template", "other", user_id="user-b")

    async def _fake_llm(self, prof, current_doc, facts):
        return "## User Profile\n### Sở thích\n- " + ", ".join(facts)
    monkeypatch.setattr(comp.MemoryCompactor, "_call_llm", _fake_llm)

    assert await comp.memory_compactor.maybe_compact(profile, user_id="user-a") is True
    # A's doc written under A; A's facts pruned; B untouched
    assert "f1" in (await profile_doc_store.get("template", user_id="user-a"))["content"]
    assert await profile_doc_store.get("template", user_id="user-b") is None
    assert await memory_store.list("template", user_id="user-a") == []
    assert [m["content"] for m in await memory_store.list("template", user_id="user-b")] == ["other"]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/unit/test_memory_compactor.py -q`
Expected: FAIL — `maybe_compact` takes no `user_id`; reads/writes the unscoped bucket.

- [ ] **Step 3: Implement**

Replace `maybe_compact` and `compact`:

```python
    async def maybe_compact(self, profile: Profile, user_id: str | None = None) -> bool:
        try:
            if not profile.memory.enabled or not profile.llm.base_url:
                return False
            items = await memory_store.list(profile.name, user_id=user_id)
            threshold = max(1, profile.memory.compaction_threshold)
            if len(items) < threshold and len(items) < profile.memory.max_facts:
                return False
            return await self.compact(profile, user_id=user_id, items=items)
        except Exception as exc:  # noqa: BLE001 - compaction is best-effort
            logger.warning("maybe_compact failed for %s: %s", profile.name, exc)
            return False

    async def compact(
        self, profile: Profile, user_id: str | None = None, items: list[dict] | None = None
    ) -> bool:
        if items is None:
            items = await memory_store.list(profile.name, user_id=user_id)
        if not items:
            return False
        items = sorted(items, key=lambda i: (i["created_at"] or "", i["id"]))
        fact_ids = [i["id"] for i in items]
        facts = [i["content"] for i in items]
        current = await profile_doc_store.get(profile.name, user_id=user_id)
        current_doc = current["content"] if current else ""
        new_doc = (await self._call_llm(profile, current_doc, facts) or "").strip()
        if not new_doc:
            logger.warning(
                "compaction produced empty doc for %s; keeping facts", profile.name
            )
            return False
        new_doc = _truncate_at_boundary(new_doc, MAX_DOC_CHARS)
        await profile_doc_store.upsert(profile.name, new_doc, user_id=user_id)
        await memory_store.delete_many(fact_ids)
        logger.info(
            "memory: compacted %d facts into profile %s", len(fact_ids), profile.name
        )
        return True
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/unit/test_memory_compactor.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/memory/compactor.py tests/unit/test_memory_compactor.py
git commit -m "feat(memory): key profile-doc compaction by user"
```

---

### Task 5: Retriever — inject only the acting user's memories

**Files:**
- Modify: `apps/api_gateway/app/services/memory/retriever.py:38-67`
- Test: `tests/unit/test_memory_retriever.py`

**Interfaces:**
- Consumes: user-scoped `memory_store`, `profile_doc_store` (Task 1).
- Produces: `MemoryRetriever.get_context(profile: Profile | None, query: str = "", user_id: str | None = None) -> str`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_memory_retriever.py`:

```python
async def test_get_context_scopes_to_user():
    from app.services.memory.retriever import memory_retriever
    from app.services.memory.store import memory_store
    from app.services.profiles.models import MemoryConfig, Profile

    profile = Profile(name="template", memory=MemoryConfig(enabled=True, mode="all"))
    await memory_store.add("template", "A likes tea", user_id="user-a")
    await memory_store.add("template", "B likes coffee", user_id="user-b")

    a_block = await memory_retriever.get_context(profile, user_id="user-a")
    assert "A likes tea" in a_block
    assert "B likes coffee" not in a_block
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/unit/test_memory_retriever.py -q`
Expected: FAIL — `get_context` takes no `user_id`; block contains both users' facts.

- [ ] **Step 3: Implement**

Change the signature and the two scoped store reads in `get_context`:

```python
    async def get_context(
        self, profile: Profile | None, query: str = "", user_id: str | None = None
    ) -> str:
        if profile is None or not profile.memory.enabled:
            return ""
        doc = await profile_doc_store.get(profile.name, user_id=user_id)
        doc_block = doc["content"].strip() if doc and doc["content"] else ""
        doc_block = _truncate_at_boundary(doc_block, MAX_DOC_CHARS)
        items = await memory_store.list(profile.name, user_id=user_id)
```

The rest of the method is unchanged.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/unit/test_memory_retriever.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/memory/retriever.py tests/unit/test_memory_retriever.py
git commit -m "feat(memory): retrieve only the acting user's memories"
```

---

### Task 6: Wire call sites — pass the resolved user through

**Files:**
- Modify: `apps/api_gateway/app/services/conversation/session.py:678` (WS teardown)
- Modify: `apps/api_gateway/app/api/routes/conversation.py` (HTTP route: `get_context` + `extract_and_upsert`)
- Test: `tests/unit/test_conversation_history.py`

**Interfaces:**
- Consumes: `extract_and_upsert(..., user_id=)` (Task 3), `get_context(..., user_id=)` (Task 5).
- WS source: `self.cfg.identity_user_id` (`SessionConfig.identity_user_id`, `session.py:120`).
- HTTP source: `current_user_id(request)` (`app.core.actor`).

- [ ] **Step 1: Verify the HTTP route has `request` and find both call sites**

Run: `grep -n "def chat\|current_user_id\|get_context\|extract_and_upsert\|request: Request\|from app.core.actor" apps/api_gateway/app/api/routes/conversation.py`

If the chat route's signature lacks `request: Request`, add it (FastAPI injects it) and add `from app.core.actor import current_user_id` to the imports. Bind once near the top of the handler: `caller_id = current_user_id(request)`.

- [ ] **Step 2: Write the failing test**

Append to `tests/unit/test_conversation_history.py` (reuse its client/profile fixtures; if it drives chat over HTTP, assert cross-user isolation end-to-end):

```python
async def test_two_users_on_one_profile_keep_separate_memory(monkeypatch):
    """Facts extracted for user A must not surface in user B's context on the
    same profile. Drives get_context directly with a shared profile."""
    from app.services.memory.retriever import memory_retriever
    from app.services.memory.store import memory_store
    from app.services.profiles.models import MemoryConfig, Profile

    profile = Profile(name="shared", memory=MemoryConfig(enabled=True, mode="all"))
    await memory_store.add("shared", "A is in Hanoi", user_id="user-a")

    assert "A is in Hanoi" in await memory_retriever.get_context(profile, user_id="user-a")
    assert "A is in Hanoi" not in await memory_retriever.get_context(profile, user_id="user-b")
```

(This is a regression guard for the wiring; the unit behavior is already covered in Task 5. Keep it — it documents the cross-user intent at the integration layer.)

- [ ] **Step 3: Run to verify it passes (wiring already sound) or fails**

Run: `.venv/bin/python -m pytest tests/unit/test_conversation_history.py -q`
Expected: PASS for the added test (it exercises the retriever directly). Proceed to wire the real call sites so production actually passes `user_id`.

- [ ] **Step 4: Wire `session.py`**

At `session.py:678`, change:

```python
                _spawn_background(memory_extractor.extract_and_upsert(self.cfg.session_id, self.profile))
```

to:

```python
                _spawn_background(
                    memory_extractor.extract_and_upsert(
                        self.cfg.session_id, self.profile, user_id=self.cfg.identity_user_id
                    )
                )
```

- [ ] **Step 5: Wire `conversation.py` (both call sites)**

Bind `caller_id = current_user_id(request)` once in the handler. Then:

- `get_context` call: `block = await memory_retriever.get_context(active_profile, query=last_user, user_id=caller_id)`
- `extract_and_upsert` call: `_spawn_background(memory_extractor.extract_and_upsert(sid, active_profile, user_id=caller_id))`

- [ ] **Step 6: Run the full memory + conversation suite**

Run: `.venv/bin/python -m pytest tests/unit/test_conversation_history.py tests/unit/test_memory_extractor.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/api_gateway/app/services/conversation/session.py apps/api_gateway/app/api/routes/conversation.py tests/unit/test_conversation_history.py
git commit -m "feat(memory): thread resolved user_id into WS and HTTP memory calls"
```

---

### Task 7: REST — scope memories to the acting user; relax the ownership gate

**Files:**
- Modify: `apps/api_gateway/app/api/routes/memories.py`
- Test: `tests/unit/test_memory_ownership.py`, `tests/unit/test_memories_routes.py`

**Interfaces:**
- Consumes: user-scoped `memory_store` (Task 1); `_visible` from `profiles.py`; `current_user_id` from `app.core.actor`.
- Behavior: read and write both gate on `_visible` (see spec). Each store call passes `user_id=current_user_id(request)`, so a caller only ever touches their own bucket on any profile they can see — including a template.

**Why relax `_can_write` → `_visible`:** the ownership commit (`ff6b798`) routed writes through `_can_write`, making a template's memories admin-only. That was correct while the bucket was shared. Now that the bucket is per-user, it would stop a user from managing their own memories on a template. `_can_write` still governs profile *config* (genuinely shared); it should not govern per-user data.

- [ ] **Step 1: Rewrite the ownership tests for the new semantics**

Replace the write-gating tests in `tests/unit/test_memory_ownership.py`. The isolation tests (listing/adding/wiping another user's memories) stay as-is — they still must 404 for a private profile. Replace the two template tests:

```python
def test_user_can_manage_own_memories_on_template(client, _with_password):
    _signup_login(client, "root", role="admin")
    client.post("/v1/profiles", json={"name": "template-a"})

    _signup_login(client, "toan", role="user")
    assert client.get(_mem_url("template-a")).status_code == 200
    assert client.post(_mem_url("template-a"), json={"content": "toan-note"}).status_code == 200
    assert [m["content"] for m in client.get(_mem_url("template-a")).json()["data"]] == ["toan-note"]


def test_template_memories_are_isolated_per_user(client, _with_password):
    _signup_login(client, "root", role="admin")
    client.post("/v1/profiles", json={"name": "template-a"})

    _signup_login(client, "a", role="user")
    client.post(_mem_url("template-a"), json={"content": "a-note"})

    _signup_login(client, "b", role="user")
    assert client.get(_mem_url("template-a")).json()["data"] == []
    client.post(_mem_url("template-a"), json={"content": "b-note"})

    _signup_login(client, "a", role="user")
    assert [m["content"] for m in client.get(_mem_url("template-a")).json()["data"]] == ["a-note"]
```

Delete `test_user_can_read_but_not_write_template_memories` and `test_admin_can_write_template_memories` (their admin-only-write premise is now wrong). Keep every private-profile 404 test unchanged.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/unit/test_memory_ownership.py -q`
Expected: FAIL — writes to a template still 404 for a non-admin (current `_can_write` gate), and reads/writes are not user-scoped so B sees A's note.

- [ ] **Step 3: Implement — gate on `_visible`, pass `user_id` to every store call**

Rewrite `memories.py`:

```python
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, field_validator

from app.api.routes.profiles import _visible
from app.core.actor import current_user_id
from app.services.memory.store import memory_store
from app.services.profiles.store import profile_store

router = APIRouter(prefix="/v1/profiles/{name}/memories", tags=["memories"])


def _require_visible(name: str, request: Request) -> str:
    """The caller may touch a profile's memories iff they can see the profile.
    The only bucket they can reach is their own (every store call is scoped to
    current_user_id), so read and write share the same gate. Returns the
    caller's user_id (normalized to '' when there is no logged-in user, e.g.
    dev-mode auth-off). Always 404 -- mirrors profiles.py so probing cannot
    enumerate other users' profile names."""
    profile = profile_store.get(name)
    user_id = current_user_id(request)
    if profile is None or not _visible(profile, user_id):
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    return user_id or ""


class MemoryRequest(BaseModel):
    content: str

    @field_validator("content")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("content must not be blank")
        return v


@router.get("")
async def list_memories(name: str, request: Request) -> dict:
    user_id = _require_visible(name, request)
    return {"success": True, "data": await memory_store.list(name, user_id=user_id)}


@router.post("")
async def add_memory(name: str, payload: MemoryRequest, request: Request) -> dict:
    user_id = _require_visible(name, request)
    row = await memory_store.add(name, payload.content, user_id=user_id)
    return {"success": True, "data": row}


@router.put("/{memory_id}")
async def update_memory(name: str, memory_id: str, payload: MemoryRequest, request: Request) -> dict:
    user_id = _require_visible(name, request)
    row = await memory_store.update(memory_id, payload.content, profile_id=name, user_id=user_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Memory '{memory_id}' not found")
    return {"success": True, "data": row}


@router.delete("/{memory_id}")
async def delete_memory(name: str, memory_id: str, request: Request) -> dict:
    user_id = _require_visible(name, request)
    if not await memory_store.delete(memory_id, profile_id=name, user_id=user_id):
        raise HTTPException(status_code=404, detail=f"Memory '{memory_id}' not found")
    return {"success": True, "data": {"id": memory_id, "deleted": True}}


@router.delete("")
async def delete_all_memories(name: str, request: Request) -> dict:
    user_id = _require_visible(name, request)
    count = await memory_store.delete_all(name, user_id=user_id)
    return {"success": True, "data": {"deleted": count}}
```

- [ ] **Step 4: Update `test_memories_routes.py` fixture**

Its `_profiles` fixture seeds template profiles (`owner_id=None`). Dev-mode auth-off means `current_user_id` is `None` → bucket `''`; the CRUD tests operate consistently in that one bucket, so they still pass unchanged. Run to confirm:

Run: `.venv/bin/python -m pytest tests/unit/test_memories_routes.py tests/unit/test_memory_ownership.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/api/routes/memories.py tests/unit/test_memory_ownership.py tests/unit/test_memories_routes.py
git commit -m "feat(memory): scope memories REST to the acting user; gate on visibility"
```

---

### Task 8: Full-suite verification

- [ ] **Step 1: Run the whole suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass (baseline before this work was 1100 passed). No new failures.

- [ ] **Step 2: Grep for any unmigrated store caller**

Run: `grep -rn "memory_store.list(\|profile_doc_store.get(\|profile_doc_store.upsert(\|extract_and_upsert(\|get_context(\|maybe_compact(" apps`
Verify every production call site passes `user_id` (or intentionally relies on the `None`→`''` default). Fix any stragglers, re-run the suite, amend the relevant commit.

- [ ] **Step 3: Manual smoke via /verify (optional but recommended)**

Drive a real WS or HTTP chat turn under two different logged-in users on one template profile; confirm memories do not cross. Use the `verify` skill.

---

## Self-Review

**Spec coverage:**
- `(user_id, profile_id)` keying → Tasks 1, 3–7. ✓
- `''` sentinel for shared-token fleet → `_uid` (Task 1), backfill (Task 2), `_require_visible` return (Task 7). ✓
- Composite PK on `MemoryProfileDoc`, `''`-not-`NULL` rationale → Task 1 (model) + Task 2 (existing-DB rebuild). ✓
- Identity threading from WS `identity_user_id` / HTTP `current_user_id` → Task 6. ✓
- Fix the `extractor.py:143` root cause (owner_id → passed user_id) → Task 3. ✓
- Migration backfills NULL→`''` and rebuilds doc PK without content loss → Task 2 (both asserted). ✓
- REST scopes to acting user; `_can_write`→`_visible` relaxation with the "may manage own on template / cannot see another's" pair → Task 7. ✓
- Out-of-scope items (conflict reconcile, dedup-vs-doc, semantic enablement, device-scoping, vector store) → untouched. ✓

**Placeholder scan:** No TBD/TODO; every code step shows full code; every run step states expected output.

**Type consistency:** `_uid` used uniformly; `user_id: str | None = None` on every scoped method; `s.get(MemoryProfileDoc, (user_id, profile_id))` matches declared PK order (`user_id` first); `get_context`/`extract_and_upsert`/`maybe_compact`/`compact` signatures consistent between the task that defines them and the task that calls them.

**Note on Task 3↔4 ordering:** Task 3's `maybe_compact(profile, user_id=...)` call precedes Task 4 defining that parameter. Task 3's tests keep the buffer below the compaction threshold, so the call short-circuits and the tests pass in isolation; Task 4 then makes the parameter real. Execute in order.
