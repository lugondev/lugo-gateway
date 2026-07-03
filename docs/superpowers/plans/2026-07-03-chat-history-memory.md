# Chat History + Per-Profile Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist chat history per session (SQLite) and long-term memory per profile (mem0-style auto-extraction + manual editing), with sessions/memories REST APIs and profile-editor UI.

**Architecture:** SQLAlchemy async ORM over `aiosqlite` (PostgreSQL-ready — swap the connection string). Three tables: `sessions`, `messages`, `memories`. Async stores wrap all DB access. After a conversation ends, a background LLM call extracts durable facts into the profile's memory; each turn injects memories into the system prompt (all, or semantic top-k when the profile opts in).

**Tech Stack:** FastAPI, SQLAlchemy 2.x async, aiosqlite, httpx, pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-07-03-chat-history-memory-design.md`

## Global Constraints

- Python `>=3.10`; run tests with the project venv: `.venv/bin/pytest`.
- New runtime deps allowed: `sqlalchemy[asyncio]>=2.0.0`, `aiosqlite>=0.20.0`. Nothing else.
- All DB access via the stores — routes and conversation code never import ORM models directly.
- Memory extraction failures must NEVER break a session teardown (log + swallow).
- API responses follow the existing envelope: `{"success": True, "data": ...}`.
- Keep SQL portable (no SQLite-only features); deletes cascade in store code, not via FK pragmas.
- Existing `profiles.json` files must still validate (new profile fields all default).
- Run the full suite (`.venv/bin/pytest tests/ -q`) before each commit; no regressions.

---

### Task 1: DB layer — engine, ORM models, settings

**Files:**
- Modify: `pyproject.toml` (add deps)
- Modify: `apps/api_gateway/app/core/settings.py` (add `database_url` near `profiles_path`, line ~194)
- Create: `apps/api_gateway/app/services/db/__init__.py`
- Create: `apps/api_gateway/app/services/db/models.py`
- Create: `apps/api_gateway/app/services/db/engine.py`
- Test: `tests/unit/test_db_engine.py`

**Interfaces:**
- Produces: `app.services.db.engine.configure(url: str | None = None) -> None` (re-point DB, resets init flag), `db_session()` (async context manager yielding `AsyncSession`, lazily runs `create_all` once), `init_db() -> None` (idempotent).
- Produces ORM models in `app.services.db.models`: `Base`, `ChatSession` (`id: str PK`, `profile_id: str`, `created_at`, `ended_at: datetime|None`, `meta: dict JSON`), `ChatMessage` (`id: int PK autoincr`, `session_id: str indexed`, `turn: int`, `role: str`, `content: str`, `created_at`), `MemoryItem` (`id: str PK`, `profile_id: str indexed`, `content: str`, `source_session_id: str|None`, `embedding: list|None JSON`, `created_at`, `updated_at`).

- [ ] **Step 1: Add dependencies**

In `pyproject.toml` `dependencies`, after `"faster-whisper>=1.1.0",` add:

```toml
  "sqlalchemy[asyncio]>=2.0.0",
  "aiosqlite>=0.20.0",
```

Run: `.venv/bin/pip install "sqlalchemy[asyncio]>=2.0.0" "aiosqlite>=0.20.0"`

- [ ] **Step 2: Add setting**

In `apps/api_gateway/app/core/settings.py`, after `mcp_servers_path: str = "mcp_servers.json"` add:

```python
    database_url: str = "sqlite+aiosqlite:///data/app.db"
```

- [ ] **Step 3: Write the failing test**

`tests/unit/test_db_engine.py`:

```python
import pytest

from app.services.db import engine as db_engine
from app.services.db.models import ChatMessage, ChatSession, MemoryItem


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    db_engine.configure(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    yield
    db_engine.configure()  # restore default for other tests


@pytest.mark.asyncio
async def test_db_session_creates_tables_lazily():
    async with db_engine.db_session() as s:
        sess = ChatSession(id="s1", profile_id="p1", meta={"a": 1})
        s.add(sess)
        await s.commit()
    async with db_engine.db_session() as s:
        got = await s.get(ChatSession, "s1")
        assert got is not None
        assert got.profile_id == "p1"
        assert got.meta == {"a": 1}
        assert got.ended_at is None


@pytest.mark.asyncio
async def test_message_and_memory_models_roundtrip():
    async with db_engine.db_session() as s:
        s.add(ChatSession(id="s2", profile_id=""))
        s.add(ChatMessage(session_id="s2", turn=1, role="user", content="hi"))
        s.add(MemoryItem(id="m1", profile_id="p1", content="fact", embedding=[0.1, 0.2]))
        await s.commit()
    async with db_engine.db_session() as s:
        mem = await s.get(MemoryItem, "m1")
        assert mem.embedding == [0.1, 0.2]
        assert mem.created_at is not None
```

- [ ] **Step 4: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_db_engine.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.db'`

- [ ] **Step 5: Implement models**

`apps/api_gateway/app/services/db/__init__.py`: empty file.

`apps/api_gateway/app/services/db/models.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class ChatSession(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    profile_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class ChatMessage(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id"), index=True
    )
    turn: Mapped[int] = mapped_column(Integer, default=0)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MemoryItem(Base):
    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    profile_id: Mapped[str] = mapped_column(String(128), index=True)
    content: Mapped[str] = mapped_column(Text)
    source_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    embedding: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
```

- [ ] **Step 6: Implement engine**

`apps/api_gateway/app/services/db/engine.py`:

```python
"""Async DB engine + session factory.

SQLite (aiosqlite) by default; PostgreSQL later is a settings.database_url
change. Tables are created lazily on first use so tests and the app need no
explicit startup hook.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.settings import settings

_engine: AsyncEngine | None = None
_factory: async_sessionmaker[AsyncSession] | None = None
_init_lock = asyncio.Lock()
_initialized = False


def configure(url: str | None = None) -> None:
    """(Re)point the DB at a URL. Tests pass a tmp path; prod uses settings."""
    global _engine, _factory, _initialized
    url = url or settings.database_url
    if url.startswith("sqlite"):
        db_file = url.split("///", 1)[-1]
        if db_file and db_file != ":memory:":
            Path(db_file).parent.mkdir(parents=True, exist_ok=True)
    _engine = create_async_engine(url)
    _factory = async_sessionmaker(_engine, expire_on_commit=False)
    _initialized = False


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
        _initialized = True


@asynccontextmanager
async def db_session():
    """Async context manager yielding an AsyncSession, init-on-first-use."""
    if not _initialized:
        await init_db()
    assert _factory is not None
    async with _factory() as session:
        yield session
```

- [ ] **Step 7: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_db_engine.py -v`
Expected: 2 PASS. (If `pytest.mark.asyncio` errors with "async def functions are not natively supported", add `asyncio_mode = "auto"` under `[tool.pytest.ini_options]` in `pyproject.toml` — check first whether existing async tests already rely on a mode.)

- [ ] **Step 8: Full suite + commit**

Run: `.venv/bin/pytest tests/ -q` — no regressions.

```bash
git add pyproject.toml apps/api_gateway/app/core/settings.py apps/api_gateway/app/services/db tests/unit/test_db_engine.py
git commit -m "feat(db): async SQLAlchemy engine + session/message/memory models"
```

---

### Task 2: SessionStore

**Files:**
- Create: `apps/api_gateway/app/services/history/__init__.py`
- Create: `apps/api_gateway/app/services/history/store.py`
- Test: `tests/unit/test_session_store.py`

**Interfaces:**
- Consumes: `db_session()` from Task 1.
- Produces: `app.services.history.store.session_store` singleton (`SessionStore`) with:
  - `async create(session_id: str, profile_id: str = "", meta: dict | None = None) -> dict`
  - `async get(session_id: str) -> dict | None` — `{id, profile_id, created_at, ended_at, meta}` (datetimes ISO strings)
  - `async exists(session_id: str) -> bool`
  - `async list(profile_id: str | None = None, limit: int = 20, offset: int = 0) -> list[dict]` — newest first, each dict adds `preview` (first user message, ≤80 chars) and `message_count`
  - `async append_message(session_id: str, turn: int, role: str, content: str) -> None`
  - `async get_messages(session_id: str) -> list[dict]` — `[{turn, role, content}]` in insert order
  - `async mark_ended(session_id: str) -> None`
  - `async delete(session_id: str) -> bool` — deletes messages then session

- [ ] **Step 1: Write the failing test**

`tests/unit/test_session_store.py`:

```python
import pytest

from app.services.db import engine as db_engine
from app.services.history.store import SessionStore


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    db_engine.configure(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    yield
    db_engine.configure()


@pytest.fixture
def store():
    return SessionStore()


@pytest.mark.asyncio
async def test_create_and_get(store):
    await store.create("s1", profile_id="pet", meta={"tts": "vieneu"})
    got = await store.get("s1")
    assert got["profile_id"] == "pet"
    assert got["meta"] == {"tts": "vieneu"}
    assert got["ended_at"] is None
    assert await store.exists("s1") is True
    assert await store.get("nope") is None


@pytest.mark.asyncio
async def test_messages_roundtrip(store):
    await store.create("s1")
    await store.append_message("s1", 1, "user", "xin chào")
    await store.append_message("s1", 1, "assistant", "chào bạn")
    msgs = await store.get_messages("s1")
    assert [(m["role"], m["content"]) for m in msgs] == [
        ("user", "xin chào"),
        ("assistant", "chào bạn"),
    ]


@pytest.mark.asyncio
async def test_list_filters_and_previews(store):
    await store.create("a", profile_id="p1")
    await store.append_message("a", 1, "user", "hello world")
    await store.create("b", profile_id="p2")
    rows = await store.list(profile_id="p1")
    assert len(rows) == 1
    assert rows[0]["id"] == "a"
    assert rows[0]["preview"] == "hello world"
    assert rows[0]["message_count"] == 1
    assert len(await store.list()) == 2


@pytest.mark.asyncio
async def test_mark_ended_and_delete(store):
    await store.create("s1")
    await store.append_message("s1", 1, "user", "x")
    await store.mark_ended("s1")
    assert (await store.get("s1"))["ended_at"] is not None
    assert await store.delete("s1") is True
    assert await store.get("s1") is None
    assert await store.get_messages("s1") == []
    assert await store.delete("s1") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_session_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.history'`

- [ ] **Step 3: Implement**

`apps/api_gateway/app/services/history/__init__.py`: empty.

`apps/api_gateway/app/services/history/store.py`:

```python
from __future__ import annotations

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select

from app.services.db.engine import db_session
from app.services.db.models import ChatMessage, ChatSession, utcnow


def _session_dict(s: ChatSession) -> dict:
    return {
        "id": s.id,
        "profile_id": s.profile_id,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "ended_at": s.ended_at.isoformat() if s.ended_at else None,
        "meta": s.meta or {},
    }


class SessionStore:
    async def create(self, session_id: str, profile_id: str = "", meta: dict | None = None) -> dict:
        async with db_session() as s:
            row = ChatSession(id=session_id, profile_id=profile_id, meta=meta or {})
            s.add(row)
            await s.commit()
            return _session_dict(row)

    async def get(self, session_id: str) -> dict | None:
        async with db_session() as s:
            row = await s.get(ChatSession, session_id)
            return _session_dict(row) if row else None

    async def exists(self, session_id: str) -> bool:
        return await self.get(session_id) is not None

    async def list(self, profile_id: str | None = None, limit: int = 20, offset: int = 0) -> list[dict]:
        async with db_session() as s:
            q = select(ChatSession).order_by(ChatSession.created_at.desc())
            if profile_id is not None:
                q = q.where(ChatSession.profile_id == profile_id)
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

    async def append_message(self, session_id: str, turn: int, role: str, content: str) -> None:
        async with db_session() as s:
            s.add(ChatMessage(session_id=session_id, turn=turn, role=role, content=content))
            await s.commit()

    async def get_messages(self, session_id: str) -> list[dict]:
        async with db_session() as s:
            rows = (
                await s.execute(
                    select(ChatMessage)
                    .where(ChatMessage.session_id == session_id)
                    .order_by(ChatMessage.id)
                )
            ).scalars().all()
            return [{"turn": m.turn, "role": m.role, "content": m.content} for m in rows]

    async def mark_ended(self, session_id: str) -> None:
        async with db_session() as s:
            row = await s.get(ChatSession, session_id)
            if row:
                row.ended_at = utcnow()
                await s.commit()

    async def delete(self, session_id: str) -> bool:
        async with db_session() as s:
            row = await s.get(ChatSession, session_id)
            if not row:
                return False
            await s.execute(sa_delete(ChatMessage).where(ChatMessage.session_id == session_id))
            await s.delete(row)
            await s.commit()
            return True


session_store = SessionStore()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_session_store.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/history tests/unit/test_session_store.py
git commit -m "feat(history): async SessionStore for per-session chat history"
```

---

### Task 3: MemoryStore

**Files:**
- Create: `apps/api_gateway/app/services/memory/__init__.py`
- Create: `apps/api_gateway/app/services/memory/store.py`
- Test: `tests/unit/test_memory_store.py`

**Interfaces:**
- Consumes: `db_session()` from Task 1.
- Produces: `app.services.memory.store.memory_store` singleton (`MemoryStore`) with:
  - `async list(profile_id: str) -> list[dict]` — newest first, `{id, profile_id, content, source_session_id, embedding, created_at, updated_at}`
  - `async add(profile_id: str, content: str, source_session_id: str | None = None, embedding: list[float] | None = None) -> dict`
  - `async update(memory_id: str, content: str) -> dict | None`
  - `async delete(memory_id: str) -> bool`
  - `async delete_all(profile_id: str) -> int`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_memory_store.py`:

```python
import pytest

from app.services.db import engine as db_engine
from app.services.memory.store import MemoryStore


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    db_engine.configure(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    yield
    db_engine.configure()


@pytest.fixture
def store():
    return MemoryStore()


@pytest.mark.asyncio
async def test_add_and_list(store):
    m = await store.add("pet", "User prefers Vietnamese", source_session_id="s1")
    assert m["id"]
    rows = await store.list("pet")
    assert len(rows) == 1
    assert rows[0]["content"] == "User prefers Vietnamese"
    assert rows[0]["source_session_id"] == "s1"
    assert await store.list("other") == []


@pytest.mark.asyncio
async def test_update_and_delete(store):
    m = await store.add("pet", "old")
    updated = await store.update(m["id"], "new")
    assert updated["content"] == "new"
    assert await store.update("ghost", "x") is None
    assert await store.delete(m["id"]) is True
    assert await store.delete(m["id"]) is False


@pytest.mark.asyncio
async def test_delete_all(store):
    await store.add("pet", "a")
    await store.add("pet", "b")
    await store.add("other", "c")
    assert await store.delete_all("pet") == 2
    assert await store.list("pet") == []
    assert len(await store.list("other")) == 1


@pytest.mark.asyncio
async def test_embedding_persists(store):
    m = await store.add("pet", "fact", embedding=[0.5, 0.5])
    rows = await store.list("pet")
    assert rows[0]["embedding"] == [0.5, 0.5]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_memory_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.memory'`

- [ ] **Step 3: Implement**

`apps/api_gateway/app/services/memory/__init__.py`: empty.

`apps/api_gateway/app/services/memory/store.py`:

```python
from __future__ import annotations

import uuid

from sqlalchemy import delete as sa_delete
from sqlalchemy import select

from app.services.db.engine import db_session
from app.services.db.models import MemoryItem, utcnow


def _mem_dict(m: MemoryItem) -> dict:
    return {
        "id": m.id,
        "profile_id": m.profile_id,
        "content": m.content,
        "source_session_id": m.source_session_id,
        "embedding": m.embedding,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }


class MemoryStore:
    async def list(self, profile_id: str) -> list[dict]:
        async with db_session() as s:
            rows = (
                await s.execute(
                    select(MemoryItem)
                    .where(MemoryItem.profile_id == profile_id)
                    .order_by(MemoryItem.created_at.desc(), MemoryItem.id)
                )
            ).scalars().all()
            return [_mem_dict(m) for m in rows]

    async def add(
        self,
        profile_id: str,
        content: str,
        source_session_id: str | None = None,
        embedding: list[float] | None = None,
    ) -> dict:
        async with db_session() as s:
            row = MemoryItem(
                id=str(uuid.uuid4()),
                profile_id=profile_id,
                content=content,
                source_session_id=source_session_id,
                embedding=embedding,
            )
            s.add(row)
            await s.commit()
            return _mem_dict(row)

    async def update(self, memory_id: str, content: str) -> dict | None:
        async with db_session() as s:
            row = await s.get(MemoryItem, memory_id)
            if not row:
                return None
            row.content = content
            row.updated_at = utcnow()
            await s.commit()
            return _mem_dict(row)

    async def delete(self, memory_id: str) -> bool:
        async with db_session() as s:
            row = await s.get(MemoryItem, memory_id)
            if not row:
                return False
            await s.delete(row)
            await s.commit()
            return True

    async def delete_all(self, profile_id: str) -> int:
        async with db_session() as s:
            result = await s.execute(
                sa_delete(MemoryItem).where(MemoryItem.profile_id == profile_id)
            )
            await s.commit()
            return result.rowcount or 0


memory_store = MemoryStore()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_memory_store.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/memory tests/unit/test_memory_store.py
git commit -m "feat(memory): async MemoryStore for per-profile memories"
```

---

### Task 4: Profile model — nickname + MemoryConfig

**Files:**
- Modify: `apps/api_gateway/app/services/profiles/models.py`
- Modify: `apps/api_gateway/app/api/routes/profiles.py` (extend `ProfileRequest`)
- Test: `tests/unit/test_profiles_models.py` (append tests)

**Interfaces:**
- Produces: `app.services.profiles.models.MemoryConfig` with fields `enabled: bool = True`, `mode: str = "all"` (`"all" | "semantic"`), `top_k: int = 5`, `extractor_model: str = ""`, `embed_model: str = ""`.
- Produces: `Profile.nickname: str = ""` and `Profile.memory: MemoryConfig`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_profiles_models.py`:

```python
def test_profile_defaults_memory_and_nickname():
    from app.services.profiles.models import Profile

    p = Profile(name="x")
    assert p.nickname == ""
    assert p.memory.enabled is True
    assert p.memory.mode == "all"
    assert p.memory.top_k == 5
    assert p.memory.extractor_model == ""
    assert p.memory.embed_model == ""


def test_profile_back_compat_old_json():
    from app.services.profiles.models import Profile

    # a profile saved before memory/nickname existed still validates
    old = {"name": "legacy", "system_prompt": "hi", "llm": {"model": "m"}}
    p = Profile.model_validate(old)
    assert p.memory.enabled is True
    assert p.nickname == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_profiles_models.py -v`
Expected: new tests FAIL — `AttributeError: 'Profile' object has no attribute 'nickname'`

- [ ] **Step 3: Implement**

In `apps/api_gateway/app/services/profiles/models.py`, add before `Profile`:

```python
class MemoryConfig(BaseModel):
    enabled: bool = True        # auto-extract memories after a session ends
    mode: str = "all"           # "all" | "semantic"
    top_k: int = 5              # semantic mode: how many memories to inject
    extractor_model: str = ""   # "" = use the profile's own LLM model
    embed_model: str = ""       # semantic mode: OpenAI-compatible embedding model
```

And extend `Profile`:

```python
class Profile(BaseModel):
    name: str
    nickname: str = ""
    llm: LlmConfig = LlmConfig()
    system_prompt: str = ""
    tts: TtsConfig = TtsConfig()
    mcp_servers: list[McpServer] = []
    memory: MemoryConfig = MemoryConfig()
```

In `apps/api_gateway/app/api/routes/profiles.py`, import `MemoryConfig` and extend `ProfileRequest`:

```python
class ProfileRequest(BaseModel):
    name: str
    nickname: str = ""
    llm: LlmConfig = LlmConfig()
    system_prompt: str = ""
    tts: TtsConfig = TtsConfig()
    mcp_servers: list[McpServer] = []
    memory: MemoryConfig = MemoryConfig()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_profiles_models.py tests/unit/test_profiles_routes.py tests/unit/test_profiles_store.py -v`
Expected: all PASS (back-compat holds)

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/profiles/models.py apps/api_gateway/app/api/routes/profiles.py tests/unit/test_profiles_models.py
git commit -m "feat(profiles): nickname + MemoryConfig (enabled/mode/top_k/models)"
```

---

### Task 5: Sessions REST routes

**Files:**
- Create: `apps/api_gateway/app/api/routes/sessions.py`
- Modify: `apps/api_gateway/app/main.py` (import + include router)
- Test: `tests/unit/test_sessions_routes.py`

**Interfaces:**
- Consumes: `session_store` from Task 2.
- Produces: `GET /v1/sessions?profile=&limit=&offset=`, `GET /v1/sessions/{session_id}` (session dict + `messages` list), `DELETE /v1/sessions/{session_id}`.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_sessions_routes.py`:

```python
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.db import engine as db_engine
from app.services.history.store import session_store


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    db_engine.configure(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    yield
    db_engine.configure()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def seeded(client):
    import asyncio

    async def _seed():
        await session_store.create("s1", profile_id="pet")
        await session_store.append_message("s1", 1, "user", "hello")
        await session_store.append_message("s1", 1, "assistant", "hi there")
        await session_store.create("s2", profile_id="other")

    asyncio.run(_seed())


def test_list_sessions(client, seeded):
    resp = client.get("/v1/sessions", params={"profile": "pet"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["id"] == "s1"
    assert data[0]["preview"] == "hello"
    assert data[0]["message_count"] == 2


def test_get_session_with_messages(client, seeded):
    resp = client.get("/v1/sessions/s1")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["profile_id"] == "pet"
    assert [m["role"] for m in data["messages"]] == ["user", "assistant"]


def test_get_missing_session_404(client):
    assert client.get("/v1/sessions/ghost").status_code == 404


def test_delete_session(client, seeded):
    resp = client.delete("/v1/sessions/s1")
    assert resp.status_code == 200
    assert resp.json()["data"]["deleted"] is True
    assert client.get("/v1/sessions/s1").status_code == 404


def test_delete_missing_404(client):
    assert client.delete("/v1/sessions/ghost").status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_sessions_routes.py -v`
Expected: FAIL — 404 on all `/v1/sessions` calls (router not registered)

- [ ] **Step 3: Implement**

`apps/api_gateway/app/api/routes/sessions.py`:

```python
from fastapi import APIRouter, HTTPException

from app.services.history.store import session_store

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])


@router.get("")
async def list_sessions(profile: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    rows = await session_store.list(profile_id=profile, limit=limit, offset=offset)
    return {"success": True, "data": rows}


@router.get("/{session_id}")
async def get_session(session_id: str) -> dict:
    sess = await session_store.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    sess["messages"] = await session_store.get_messages(session_id)
    return {"success": True, "data": sess}


@router.delete("/{session_id}")
async def delete_session(session_id: str) -> dict:
    if not await session_store.delete(session_id):
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return {"success": True, "data": {"id": session_id, "deleted": True}}
```

In `apps/api_gateway/app/main.py`, add import (alphabetical, after `recommend`):

```python
from app.api.routes.sessions import router as sessions_router
```

and after `app.include_router(mcp_router)`:

```python
app.include_router(sessions_router)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_sessions_routes.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/api/routes/sessions.py apps/api_gateway/app/main.py tests/unit/test_sessions_routes.py
git commit -m "feat(sessions): REST routes to list/get/delete chat sessions"
```

---

### Task 6: Memories REST routes

**Files:**
- Create: `apps/api_gateway/app/api/routes/memories.py`
- Modify: `apps/api_gateway/app/main.py` (import + include router)
- Test: `tests/unit/test_memories_routes.py`

**Interfaces:**
- Consumes: `memory_store` from Task 3.
- Produces: under `/v1/profiles/{name}/memories`: `GET ""`, `POST "" {content}`, `PUT "/{memory_id}" {content}`, `DELETE "/{memory_id}"`, `DELETE ""` (all for profile).

- [ ] **Step 1: Write the failing test**

`tests/unit/test_memories_routes.py`:

```python
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.db import engine as db_engine


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    db_engine.configure(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    yield
    db_engine.configure()


@pytest.fixture
def client():
    return TestClient(app)


def test_memories_crud(client):
    # empty
    assert client.get("/v1/profiles/pet/memories").json()["data"] == []
    # add
    resp = client.post("/v1/profiles/pet/memories", json={"content": "likes tea"})
    assert resp.status_code == 200
    mid = resp.json()["data"]["id"]
    # list
    rows = client.get("/v1/profiles/pet/memories").json()["data"]
    assert len(rows) == 1 and rows[0]["content"] == "likes tea"
    # edit
    resp = client.put(f"/v1/profiles/pet/memories/{mid}", json={"content": "likes coffee"})
    assert resp.json()["data"]["content"] == "likes coffee"
    # edit missing -> 404
    assert client.put("/v1/profiles/pet/memories/ghost", json={"content": "x"}).status_code == 404
    # delete one
    assert client.delete(f"/v1/profiles/pet/memories/{mid}").json()["data"]["deleted"] is True
    assert client.delete(f"/v1/profiles/pet/memories/{mid}").status_code == 404


def test_delete_all_memories(client):
    client.post("/v1/profiles/pet/memories", json={"content": "a"})
    client.post("/v1/profiles/pet/memories", json={"content": "b"})
    resp = client.delete("/v1/profiles/pet/memories")
    assert resp.json()["data"]["deleted"] == 2
    assert client.get("/v1/profiles/pet/memories").json()["data"] == []


def test_empty_content_rejected(client):
    assert client.post("/v1/profiles/pet/memories", json={"content": "  "}).status_code == 422
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_memories_routes.py -v`
Expected: FAIL — 404 (router not registered)

- [ ] **Step 3: Implement**

`apps/api_gateway/app/api/routes/memories.py`:

```python
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from app.services.memory.store import memory_store

router = APIRouter(prefix="/v1/profiles/{name}/memories", tags=["memories"])


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
async def list_memories(name: str) -> dict:
    return {"success": True, "data": await memory_store.list(name)}


@router.post("")
async def add_memory(name: str, payload: MemoryRequest) -> dict:
    row = await memory_store.add(name, payload.content)
    return {"success": True, "data": row}


@router.put("/{memory_id}")
async def update_memory(name: str, memory_id: str, payload: MemoryRequest) -> dict:
    row = await memory_store.update(memory_id, payload.content)
    if not row:
        raise HTTPException(status_code=404, detail=f"Memory '{memory_id}' not found")
    return {"success": True, "data": row}


@router.delete("/{memory_id}")
async def delete_memory(name: str, memory_id: str) -> dict:
    if not await memory_store.delete(memory_id):
        raise HTTPException(status_code=404, detail=f"Memory '{memory_id}' not found")
    return {"success": True, "data": {"id": memory_id, "deleted": True}}


@router.delete("")
async def delete_all_memories(name: str) -> dict:
    count = await memory_store.delete_all(name)
    return {"success": True, "data": {"deleted": count}}
```

In `apps/api_gateway/app/main.py` add:

```python
from app.api.routes.memories import router as memories_router
```

and:

```python
app.include_router(memories_router)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_memories_routes.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/api/routes/memories.py apps/api_gateway/app/main.py tests/unit/test_memories_routes.py
git commit -m "feat(memories): per-profile memory CRUD routes"
```

---

### Task 7: Memory extractor

**Files:**
- Create: `apps/api_gateway/app/services/memory/extractor.py`
- Test: `tests/unit/test_memory_extractor.py`

**Interfaces:**
- Consumes: `session_store.get_messages` (Task 2), `memory_store.list/add` (Task 3), `Profile` with `memory` config (Task 4).
- Produces: `app.services.memory.extractor.memory_extractor` singleton (`MemoryExtractor`) with:
  - `async extract(messages: list[dict], base_url: str, api_key: str, model: str) -> list[str]` — one LLM call, parses a JSON array of fact strings (tolerates surrounding prose/markdown fences), returns `[]` on any failure.
  - `async extract_and_upsert(session_id: str, profile: Profile) -> int` — loads session messages, skips if `<2` messages or no `profile.llm.base_url`, dedupes case-insensitively against existing memories, adds new ones with `source_session_id`, returns count added. Never raises.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_memory_extractor.py`:

```python
import pytest

from app.services.db import engine as db_engine
from app.services.memory.extractor import MemoryExtractor, _parse_facts
from app.services.memory.store import memory_store
from app.services.history.store import session_store
from app.services.profiles.models import Profile


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    db_engine.configure(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    yield
    db_engine.configure()


def test_parse_facts_plain_array():
    assert _parse_facts('["a", "b"]') == ["a", "b"]


def test_parse_facts_fenced_and_prose():
    raw = 'Here you go:\n```json\n["User likes tea", "User is a dev"]\n```'
    assert _parse_facts(raw) == ["User likes tea", "User is a dev"]


def test_parse_facts_garbage_returns_empty():
    assert _parse_facts("no json here") == []
    assert _parse_facts('{"not": "an array"}') == []
    assert _parse_facts('[1, 2, {"x": 3}]') == []


@pytest.mark.asyncio
async def test_extract_calls_llm(monkeypatch):
    captured = {}

    async def fake_post(self, url, headers=None, json=None):
        captured["url"] = url
        captured["json"] = json

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": '["User speaks Vietnamese"]'}}]}

        return R()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    ex = MemoryExtractor()
    facts = await ex.extract(
        [{"role": "user", "content": "xin chào"}, {"role": "assistant", "content": "chào"}],
        base_url="http://llm.local/v1", api_key="k", model="m",
    )
    assert facts == ["User speaks Vietnamese"]
    assert captured["url"] == "http://llm.local/v1/chat/completions"
    assert captured["json"]["model"] == "m"


@pytest.mark.asyncio
async def test_extract_and_upsert_dedupes(monkeypatch):
    await session_store.create("s1", profile_id="pet")
    await session_store.append_message("s1", 1, "user", "tôi thích trà")
    await session_store.append_message("s1", 1, "assistant", "ok")
    await memory_store.add("pet", "user likes tea")

    async def fake_extract(self, messages, base_url, api_key, model):
        return ["User Likes Tea", "User is from Hanoi"]

    monkeypatch.setattr(MemoryExtractor, "extract", fake_extract)
    profile = Profile(name="pet", llm={"base_url": "http://llm.local/v1", "model": "m"})
    added = await MemoryExtractor().extract_and_upsert("s1", profile)
    assert added == 1  # tea fact deduped case-insensitively
    contents = {m["content"] for m in await memory_store.list("pet")}
    assert "User is from Hanoi" in contents


@pytest.mark.asyncio
async def test_extract_and_upsert_skips_short_or_no_llm():
    await session_store.create("s2", profile_id="pet")
    await session_store.append_message("s2", 1, "user", "hi")
    profile = Profile(name="pet", llm={"base_url": "http://llm.local/v1", "model": "m"})
    assert await MemoryExtractor().extract_and_upsert("s2", profile) == 0  # <2 messages
    no_llm = Profile(name="pet")
    assert await MemoryExtractor().extract_and_upsert("s1", no_llm) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_memory_extractor.py -v`
Expected: FAIL — `ImportError: cannot import name 'MemoryExtractor'`

- [ ] **Step 3: Implement**

`apps/api_gateway/app/services/memory/extractor.py`:

```python
"""Post-session memory extraction (mem0-inspired).

One LLM call over the session transcript -> JSON array of durable user facts
-> deduped upsert into MemoryStore. Failures are logged and swallowed: memory
extraction must never break a session teardown.
"""

from __future__ import annotations

import json
import logging
import re

import httpx

from app.core.settings import settings
from app.services.history.store import session_store
from app.services.memory.store import memory_store
from app.services.profiles.models import Profile

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = (
    "You extract durable facts about the user from a conversation transcript. "
    "Return ONLY a JSON array of short fact strings, in the user's language, "
    'e.g. ["User prefers Vietnamese", "User is building an ESP32 assistant"]. '
    "Only include stable facts worth remembering across conversations "
    "(preferences, identity, projects, constraints, relationships). "
    "Do not include small talk or one-off requests. Return [] if none."
)

_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


def _parse_facts(raw: str) -> list[str]:
    """Extract a JSON array of strings from an LLM reply (tolerant of prose/fences)."""
    match = _ARRAY_RE.search(raw or "")
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    facts = [item.strip() for item in data if isinstance(item, str) and item.strip()]
    return facts if len(facts) == len(data) else []


class MemoryExtractor:
    async def extract(
        self, messages: list[dict], base_url: str, api_key: str, model: str
    ) -> list[str]:
        transcript = "\n".join(
            f"{m['role']}: {m['content']}"
            for m in messages
            if m.get("role") in ("user", "assistant") and m.get("content")
        )
        if not transcript:
            return []
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        try:
            async with httpx.AsyncClient(
                timeout=settings.conversation_llm_timeout_seconds
            ) as client:
                resp = await client.post(
                    f"{base_url.rstrip('/')}/chat/completions",
                    headers=headers,
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": EXTRACTION_PROMPT},
                            {"role": "user", "content": transcript},
                        ],
                    },
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001 - extraction is best-effort
            logger.warning("memory extraction LLM call failed: %s", exc)
            return []
        return _parse_facts(str(content))

    async def extract_and_upsert(self, session_id: str, profile: Profile) -> int:
        """Extract facts from a finished session into the profile's memory."""
        try:
            if not profile.memory.enabled or not profile.llm.base_url:
                return 0
            messages = await session_store.get_messages(session_id)
            if len(messages) < 2:
                return 0
            model = profile.memory.extractor_model or profile.llm.model
            facts = await self.extract(
                messages, profile.llm.base_url, profile.llm.api_key, model
            )
            if not facts:
                return 0
            existing = {
                m["content"].strip().lower() for m in await memory_store.list(profile.name)
            }
            added = 0
            for fact in facts:
                if fact.strip().lower() in existing:
                    continue
                await memory_store.add(profile.name, fact, source_session_id=session_id)
                existing.add(fact.strip().lower())
                added += 1
            if added:
                logger.info("memory: added %d facts for profile %s", added, profile.name)
            return added
        except Exception as exc:  # noqa: BLE001 - never break session teardown
            logger.warning("extract_and_upsert failed for %s: %s", session_id, exc)
            return 0


memory_extractor = MemoryExtractor()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_memory_extractor.py -v`
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/memory/extractor.py tests/unit/test_memory_extractor.py
git commit -m "feat(memory): LLM fact extraction from finished sessions with dedupe"
```

---

### Task 8: Memory retriever + embedder

**Files:**
- Create: `apps/api_gateway/app/services/memory/embedder.py`
- Create: `apps/api_gateway/app/services/memory/retriever.py`
- Test: `tests/unit/test_memory_retriever.py`

**Interfaces:**
- Consumes: `memory_store.list` (Task 3), `Profile.memory` (Task 4).
- Produces `app.services.memory.embedder`:
  - `async embed_texts(texts: list[str], base_url: str, api_key: str, model: str) -> list[list[float]]` — OpenAI-compatible `POST {base_url}/embeddings`; raises on failure.
  - `cosine(a: list[float], b: list[float]) -> float`
- Produces `app.services.memory.retriever`:
  - `inject_memories(system_prompt: str, block: str) -> str` — prepends block + blank line; returns prompt unchanged if block empty.
  - `memory_retriever` singleton (`MemoryRetriever`) with `async get_context(profile: Profile | None, query: str = "") -> str` — returns `"## User Memories\n- ..."` block or `""`. Mode `"all"`: newest first, capped at 50 items / 2000 chars. Mode `"semantic"`: cosine top-k over stored embeddings; falls back to "all" behavior when no embeddings, no `embed_model`, or the embed call fails.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_memory_retriever.py`:

```python
import pytest

from app.services.db import engine as db_engine
from app.services.memory.embedder import cosine
from app.services.memory.retriever import MemoryRetriever, inject_memories
from app.services.memory.store import memory_store
from app.services.profiles.models import Profile


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    db_engine.configure(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    yield
    db_engine.configure()


def test_cosine():
    assert cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine([0, 0], [1, 1]) == 0.0  # zero vector guard


def test_inject_memories():
    assert inject_memories("base", "") == "base"
    out = inject_memories("base", "## User Memories\n- x")
    assert out.startswith("## User Memories\n- x")
    assert out.endswith("base")
    assert inject_memories("", "## User Memories\n- x") == "## User Memories\n- x"


@pytest.mark.asyncio
async def test_get_context_all_mode():
    await memory_store.add("pet", "likes tea")
    await memory_store.add("pet", "from Hanoi")
    profile = Profile(name="pet")
    block = await MemoryRetriever().get_context(profile)
    assert block.startswith("## User Memories")
    assert "- likes tea" in block and "- from Hanoi" in block


@pytest.mark.asyncio
async def test_get_context_empty_cases():
    r = MemoryRetriever()
    assert await r.get_context(None) == ""
    assert await r.get_context(Profile(name="empty")) == ""
    disabled = Profile(name="pet", memory={"enabled": False})
    await memory_store.add("pet", "x")
    assert await r.get_context(disabled) == ""


@pytest.mark.asyncio
async def test_semantic_mode_top_k(monkeypatch):
    await memory_store.add("pet", "likes tea", embedding=[1.0, 0.0])
    await memory_store.add("pet", "plays guitar", embedding=[0.0, 1.0])

    async def fake_embed(texts, base_url, api_key, model):
        return [[1.0, 0.0]]  # query vector ~ "tea"

    monkeypatch.setattr("app.services.memory.retriever.embed_texts", fake_embed)
    profile = Profile(
        name="pet",
        llm={"base_url": "http://llm.local/v1"},
        memory={"mode": "semantic", "top_k": 1, "embed_model": "emb"},
    )
    block = await MemoryRetriever().get_context(profile, query="tea?")
    assert "- likes tea" in block
    assert "guitar" not in block


@pytest.mark.asyncio
async def test_semantic_falls_back_when_no_embeddings():
    await memory_store.add("pet", "no vector here")
    profile = Profile(
        name="pet",
        llm={"base_url": "http://llm.local/v1"},
        memory={"mode": "semantic", "embed_model": "emb"},
    )
    block = await MemoryRetriever().get_context(profile, query="anything")
    assert "- no vector here" in block
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_memory_retriever.py -v`
Expected: FAIL — `ModuleNotFoundError` for embedder/retriever

- [ ] **Step 3: Implement embedder**

`apps/api_gateway/app/services/memory/embedder.py`:

```python
from __future__ import annotations

import math

import httpx

from app.core.settings import settings


async def embed_texts(
    texts: list[str], base_url: str, api_key: str, model: str
) -> list[list[float]]:
    """Embed texts via an OpenAI-compatible /embeddings endpoint. Raises on failure."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=settings.conversation_llm_timeout_seconds) as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/embeddings",
            headers=headers,
            json={"model": model, "input": texts},
        )
        resp.raise_for_status()
        data = resp.json()["data"]
    return [d["embedding"] for d in data]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)
```

- [ ] **Step 4: Implement retriever**

`apps/api_gateway/app/services/memory/retriever.py`:

```python
"""Build the memory context block injected into the system prompt each turn."""

from __future__ import annotations

import logging

from app.services.memory.embedder import cosine, embed_texts
from app.services.memory.store import memory_store
from app.services.profiles.models import Profile

logger = logging.getLogger(__name__)

MAX_ITEMS = 50
MAX_CHARS = 2000


def inject_memories(system_prompt: str, block: str) -> str:
    if not block:
        return system_prompt
    if not system_prompt:
        return block
    return f"{block}\n\n{system_prompt}"


class MemoryRetriever:
    async def get_context(self, profile: Profile | None, query: str = "") -> str:
        if profile is None or not profile.memory.enabled:
            return ""
        items = await memory_store.list(profile.name)
        if not items:
            return ""
        if profile.memory.mode == "semantic" and query:
            items = await self._semantic_filter(items, query, profile)
        contents: list[str] = []
        total = 0
        for item in items[:MAX_ITEMS]:
            content = item["content"]
            if total + len(content) > MAX_CHARS:
                break
            contents.append(content)
            total += len(content)
        if not contents:
            return ""
        return "## User Memories\n" + "\n".join(f"- {c}" for c in contents)

    async def _semantic_filter(
        self, items: list[dict], query: str, profile: Profile
    ) -> list[dict]:
        """Top-k by cosine similarity; falls back to the full list on any gap."""
        with_vec = [i for i in items if i.get("embedding")]
        if not with_vec or not profile.memory.embed_model or not profile.llm.base_url:
            return items
        try:
            qvec = (
                await embed_texts(
                    [query], profile.llm.base_url, profile.llm.api_key,
                    profile.memory.embed_model,
                )
            )[0]
        except Exception as exc:  # noqa: BLE001 - fall back to all memories
            logger.warning("semantic memory embed failed, using all: %s", exc)
            return items
        scored = sorted(
            with_vec, key=lambda i: cosine(qvec, i["embedding"]), reverse=True
        )
        return scored[: profile.memory.top_k]


memory_retriever = MemoryRetriever()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_memory_retriever.py -v`
Expected: 6 PASS

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/services/memory/embedder.py apps/api_gateway/app/services/memory/retriever.py tests/unit/test_memory_retriever.py
git commit -m "feat(memory): retriever with all/semantic modes + OpenAI-compat embedder"
```

---

### Task 9: Conversation wiring — /chat and WS persistence + memory injection

**Files:**
- Modify: `apps/api_gateway/app/api/routes/conversation.py`
- Test: `tests/unit/test_conversation_history.py`

**Interfaces:**
- Consumes: `session_store` (Task 2), `memory_retriever`/`inject_memories` (Task 8), `memory_extractor` (Task 7).
- Produces:
  - `POST /v1/conversation/chat?profile=&session_id=` — creates or resumes a session; stored messages prefix the LLM context; new user messages + reply are persisted; response `data` gains `"session_id"`.
  - `WS /v1/conversation/stream?...&session_id=` — resumes (seeds `history` from DB) or creates; every turn's user/assistant messages persisted; `session_started` keeps its existing `session_id` field (now DB-backed); on disconnect `mark_ended` + fire-and-forget memory extraction.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_conversation_history.py`:

```python
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.db import engine as db_engine
from app.services.history.store import session_store
from app.services.memory.store import memory_store
from app.services.profiles.store import ProfileStore
from app.services.profiles.models import Profile


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    db_engine.configure(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    yield
    db_engine.configure()


@pytest.fixture(autouse=True)
def _profiles(tmp_path, monkeypatch):
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    fresh.upsert(Profile(name="pet"))
    monkeypatch.setattr("app.api.routes.conversation.profile_store", fresh)


@pytest.fixture
def client():
    return TestClient(app)


def test_chat_creates_session_and_persists(client):
    resp = client.post(
        "/v1/conversation/chat",
        params={"profile": "pet"},
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert resp.status_code == 200
    sid = resp.json()["data"]["session_id"]
    assert sid
    msgs = asyncio.run(session_store.get_messages(sid))
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "hello"


def test_chat_resumes_session_with_stored_context(client):
    r1 = client.post(
        "/v1/conversation/chat",
        params={"profile": "pet"},
        json={"messages": [{"role": "user", "content": "first"}]},
    )
    sid = r1.json()["data"]["session_id"]
    r2 = client.post(
        "/v1/conversation/chat",
        params={"profile": "pet", "session_id": sid},
        json={"messages": [{"role": "user", "content": "second"}]},
    )
    assert r2.json()["data"]["session_id"] == sid
    msgs = asyncio.run(session_store.get_messages(sid))
    contents = [m["content"] for m in msgs if m["role"] == "user"]
    assert contents == ["first", "second"]


def test_chat_injects_memories_into_prompt(client, monkeypatch):
    asyncio.run(memory_store.add("pet", "User's name is Lugon"))
    seen = {}
    from app.services.conversation import responder as responder_mod

    orig = responder_mod.build_responder_ex

    def spy(**kwargs):
        seen.update(kwargs)
        return orig(**kwargs)

    monkeypatch.setattr("app.api.routes.conversation.build_responder_ex", spy)
    client.post(
        "/v1/conversation/chat",
        params={"profile": "pet"},
        json={"messages": [{"role": "user", "content": "who am I?"}]},
    )
    assert "User's name is Lugon" in (seen.get("system_prompt") or "")


def test_ws_persists_and_resumes(client):
    with client.websocket_connect("/v1/conversation/stream?output=text") as ws:
        started = ws.receive_json()
        assert started["event"] == "session_started"
        sid = started["session_id"]
        ws.send_json({"type": "text", "text": "xin chào"})
        while True:
            evt = ws.receive_json()
            if evt["event"] == "turn_done":
                break
    msgs = asyncio.run(session_store.get_messages(sid))
    roles = [m["role"] for m in msgs]
    assert roles[0] == "user" and "assistant" in roles

    # resume: history seeded from DB
    with client.websocket_connect(
        f"/v1/conversation/stream?output=text&session_id={sid}"
    ) as ws:
        started = ws.receive_json()
        assert started["session_id"] == sid
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_conversation_history.py -v`
Expected: FAIL — `KeyError: 'session_id'` in chat response / persistence assertions

- [ ] **Step 3: Wire /chat**

In `apps/api_gateway/app/api/routes/conversation.py` add imports:

```python
from app.services.history.store import session_store
from app.services.memory.extractor import memory_extractor
from app.services.memory.retriever import inject_memories, memory_retriever
```

Replace the `chat` route:

```python
@router.post("/chat")
async def chat(payload: ChatRequest, profile: str | None = None, session_id: str | None = None) -> dict:
    """Text chat with the configured conversation responder (LLM or echo)."""
    active_profile = profile_store.get(profile) if profile else None
    llm_base_url = (active_profile.llm.base_url or None) if (active_profile and active_profile.llm.base_url) else None
    llm_api_key = active_profile.llm.api_key if (active_profile and active_profile.llm.base_url) else None
    llm_model = (active_profile.llm.model or None) if (active_profile and active_profile.llm.model) else None
    system_prompt = (active_profile.system_prompt or None) if (active_profile and active_profile.system_prompt) else None

    # Session: resume when session_id given (stored messages prefix the context).
    sid = session_id or str(uuid.uuid4())
    stored: list[dict] = []
    if session_id and await session_store.exists(session_id):
        stored = await session_store.get_messages(session_id)
    elif not await session_store.exists(sid):
        await session_store.create(sid, profile_id=profile or "")

    new_msgs = [{"role": m.role, "content": m.content} for m in payload.messages]
    history = [{"role": m["role"], "content": m["content"]} for m in stored] + new_msgs

    # Memory injection: prepend the profile's memories to the system prompt.
    last_user = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
    block = await memory_retriever.get_context(active_profile, query=last_user)
    system_prompt = inject_memories(system_prompt or settings.conversation_system_prompt, block) if block else system_prompt

    responder = build_responder_ex(
        base_url=llm_base_url,
        api_key=llm_api_key,
        model=llm_model,
        system_prompt=system_prompt,
    )
    reply = await responder.reply(history)

    turn = (len(stored) // 2) + 1
    for m in new_msgs:
        await session_store.append_message(sid, turn, m["role"], m["content"])
    await session_store.append_message(sid, turn, "assistant", reply)
    if active_profile and active_profile.memory.enabled and active_profile.llm.base_url:
        asyncio.create_task(memory_extractor.extract_and_upsert(sid, active_profile))

    return {
        "success": True,
        "data": {
            "reply": reply,
            "responder": responder.name,
            "model": get_active_llm_model(),
            "profile": profile,
            "session_id": sid,
        },
    }
```

- [ ] **Step 4: Wire the WebSocket handler**

In `conversation_stream`, replace `session_id = str(uuid.uuid4())` (line ~122) with:

```python
    requested_sid = websocket.query_params.get("session_id")
    session_id = requested_sid or str(uuid.uuid4())
```

After the profile-resolution block (after the `warning` send for a missing profile), add session setup — replace `history: list[dict] = []` (line ~258) with:

```python
    # Session persistence: resume seeds history from the DB; new sessions are recorded.
    history: list[dict] = []
    if requested_sid and await session_store.exists(requested_sid):
        history = [
            {"role": m["role"], "content": m["content"]}
            for m in await session_store.get_messages(requested_sid)
        ]
    else:
        await session_store.create(
            session_id,
            profile_id=profile_name or "",
            meta={"stt_engine": stt_engine, "tts_engine": tts_engine},
        )
```

(Note: this block must come AFTER `stt_engine`/`tts_engine` are resolved — place it right before `turn = 0`.)

Add a persistence helper right after `async def send(...)` (line ~297):

```python
    base_system_prompt = system_prompt or settings.conversation_system_prompt

    async def persist(role: str, content: str) -> None:
        try:
            await session_store.append_message(session_id, turn, role, content)
        except Exception as exc:  # noqa: BLE001 - persistence must not kill the turn
            logger.warning("history persist failed: %s", exc)

    async def refresh_memory(query: str) -> None:
        """Per-turn memory injection (mutates the responder's system prompt)."""
        if not hasattr(responder, "system_prompt"):
            return
        try:
            block = await memory_retriever.get_context(profile, query=query)
            responder.system_prompt = inject_memories(base_system_prompt, block)
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory retrieval failed: %s", exc)
```

Then add persistence + memory calls at each history mutation site in `_run_turn`:

1. Text-input turn — after `history.append({"role": "user", "content": user_text})`:

```python
            await persist("user", user_text)
            await refresh_memory(user_text)
```

and after its `history.append({"role": "assistant", "content": " ".join(parts)})`:

```python
            await persist("assistant", " ".join(parts))
```

2. Audio-native (qwen_omni) branch — after `history.append({"role": "user", "content": transcript})`:

```python
                    await persist("user", transcript)
```

and after its assistant append:

```python
                await persist("assistant", " ".join(parts))
```

3. Normal STT path — after `history.append({"role": "user", "content": user_text})`:

```python
        await persist("user", user_text)
        await refresh_memory(user_text)
```

and after the final assistant append:

```python
        await persist("assistant", " ".join(parts))
```

Finally, in the `finally:` block (line ~556), before closing the websocket:

```python
        await session_store.mark_ended(session_id)
        if profile is not None:
            asyncio.create_task(memory_extractor.extract_and_upsert(session_id, profile))
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/pytest tests/unit/test_conversation_history.py tests/unit/test_conversation.py tests/unit/test_conversation_profile.py -v`
Expected: all PASS (existing conversation tests unaffected — echo responder has no `system_prompt` attr, so `refresh_memory` no-ops)

- [ ] **Step 6: Full suite + commit**

Run: `.venv/bin/pytest tests/ -q`

```bash
git add apps/api_gateway/app/api/routes/conversation.py tests/unit/test_conversation_history.py
git commit -m "feat(conversation): session persistence, resume, memory injection + post-session extraction"
```

---

### Task 10: UI — profile panel (nickname + memory) and sessions list

**Files:**
- Modify: `apps/api_gateway/app/static/index.html`
- Modify: `apps/api_gateway/app/static/app.js`
- Modify: `apps/api_gateway/app/static/styles.css`

**Interfaces:**
- Consumes: `/v1/profiles/{name}/memories` CRUD (Task 6), `/v1/sessions` (Task 5), `session_id` params (Task 9), profile `nickname`/`memory` fields (Task 4).
- Produces: browser UI only; no exported symbols.

Note: `index.html` and `styles.css` have uncommitted changes from earlier UI polish — commit or review those separately before starting; this task's diff must be reviewable on its own.

- [ ] **Step 1: index.html — profile panel additions**

In the profile panel `pf-left` column (after the Name label, `index.html:112-115`), add:

```html
                  <label>
                    Nickname <span class="hint" style="display:inline;margin:0">(display name)</span>
                    <input id="pf-nickname" type="text" placeholder="My Pet" />
                  </label>
```

After the MCP list (`<div id="pf-mcp-list" ...>`), add the memory section:

```html
                  <p class="field-label">Memory</p>
                  <div class="pf-memory">
                    <label class="check">
                      <input type="checkbox" id="pf-mem-enabled" checked /> Auto-extract memory after sessions
                    </label>
                    <label>
                      Retrieval mode
                      <select id="pf-mem-mode">
                        <option value="all">All memories</option>
                        <option value="semantic">Semantic (top-k)</option>
                      </select>
                    </label>
                    <div id="pf-mem-list" class="pf-mem-list"></div>
                    <div class="row tight">
                      <input id="pf-mem-new" type="text" placeholder="Add a memory fact&#8230;" />
                      <button id="pf-mem-add" class="mini" type="button">+ Add</button>
                    </div>
                  </div>
```

- [ ] **Step 2: index.html — sessions control in the profile bar**

Inside `.profile-bar-btns` (`index.html:97-100`), before the Edit button add:

```html
                <button id="session-list-btn" class="ghost mini">Sessions</button>
                <button id="session-new-btn" class="ghost mini">New session</button>
```

After the profile bar `</div>` add a hidden sessions panel:

```html
            <div class="session-panel hidden" id="session-panel">
              <div class="profile-panel-hd">
                <h3>Sessions</h3>
                <button id="session-close-btn" class="ghost mini">&#10005;</button>
              </div>
              <div id="session-list" class="session-list"></div>
            </div>
```

- [ ] **Step 3: styles.css — minimal styles**

Append (reusing existing token variables used elsewhere in the file — match the file's conventions):

```css
/* Memory list in profile panel */
.pf-mem-list { display: flex; flex-direction: column; gap: 6px; margin: 8px 0; max-height: 180px; overflow-y: auto; }
.pf-mem-item { display: flex; align-items: center; gap: 8px; font-size: 0.85rem; }
.pf-mem-item .mem-text { flex: 1; }
.pf-mem-item button { flex: none; }

/* Sessions panel */
.session-panel { margin: 8px 0; }
.session-list { display: flex; flex-direction: column; gap: 4px; max-height: 240px; overflow-y: auto; }
.session-row { display: flex; gap: 10px; align-items: baseline; cursor: pointer; padding: 6px 8px; border-radius: 6px; }
.session-row:hover { background: rgba(127, 127, 127, 0.12); }
.session-row .sess-time { flex: none; font-size: 0.75rem; opacity: 0.7; }
.session-row .sess-preview { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
```

(If the panel looks unstyled, mirror the existing `.profile-panel` card styles for `.session-panel`.)

- [ ] **Step 4: app.js — profile panel wiring**

In `openProfilePanel(mode, name)` (`app.js:1929`): populate `pf-nickname`, `pf-mem-enabled`, `pf-mem-mode` from the fetched profile (`p.nickname`, `p.memory?.enabled ?? true`, `p.memory?.mode || "all"`), defaulting for "new" mode; call `loadMemories(name)` in edit mode and clear `#pf-mem-list` in new mode.

In `saveProfile` (bound at `app.js:2070`): include in the payload:

```javascript
    nickname: el("pf-nickname").value.trim(),
    memory: {
      enabled: el("pf-mem-enabled").checked,
      mode: el("pf-mem-mode").value,
    },
```

Add memory CRUD helpers (near the profile functions):

```javascript
async function loadMemories(name) {
  const list = el("pf-mem-list");
  if (!list) return;
  list.innerHTML = "";
  if (!name) return;
  try {
    const body = await (await fetch(`/v1/profiles/${encodeURIComponent(name)}/memories`)).json();
    for (const m of body.data || []) list.appendChild(memRow(name, m));
    if (!(body.data || []).length) list.innerHTML = '<p class="hint">No memories yet.</p>';
  } catch (e) {
    list.innerHTML = '<p class="hint">Failed to load memories.</p>';
  }
}

function memRow(name, m) {
  const row = document.createElement("div");
  row.className = "pf-mem-item";
  const text = document.createElement("span");
  text.className = "mem-text";
  text.textContent = m.content;
  const edit = document.createElement("button");
  edit.className = "ghost mini";
  edit.textContent = "✎";
  edit.onclick = async () => {
    const next = prompt("Edit memory:", m.content);
    if (next === null || !next.trim()) return;
    await fetch(`/v1/profiles/${encodeURIComponent(name)}/memories/${m.id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: next.trim() }),
    });
    loadMemories(name);
  };
  const del = document.createElement("button");
  del.className = "ghost mini";
  del.textContent = "✕";
  del.onclick = async () => {
    await fetch(`/v1/profiles/${encodeURIComponent(name)}/memories/${m.id}`, { method: "DELETE" });
    loadMemories(name);
  };
  row.append(text, edit, del);
  return row;
}

if (el("pf-mem-add")) el("pf-mem-add").addEventListener("click", async () => {
  const name = el("pf-name").value.trim();
  const content = el("pf-mem-new").value.trim();
  if (!name || !content) return;
  await fetch(`/v1/profiles/${encodeURIComponent(name)}/memories`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  });
  el("pf-mem-new").value = "";
  loadMemories(name);
});
```

- [ ] **Step 5: app.js — session tracking + sessions panel**

Add near the chat state:

```javascript
let currentSessionId = null;
```

- In the text-chat send path (where `/v1/conversation/chat` is POSTed, `app.js:~1600`): append `&session_id=${currentSessionId}` to the query when set, and after the response set `currentSessionId = body.data.session_id`.
- In the WS connect path (voice modes, `app.js:~1778`): append `&session_id=${encodeURIComponent(currentSessionId)}` when set; on `session_started` set `currentSessionId = msg.session_id`.
- On profile change (`profile-select` change handler) and on `session-new-btn` click: `currentSessionId = null;` and clear `#chat-dialogue`.

Sessions panel:

```javascript
async function openSessionsPanel() {
  const panel = el("session-panel");
  const list = el("session-list");
  panel.classList.remove("hidden");
  list.innerHTML = '<p class="hint">Loading&#8230;</p>';
  const profile = el("profile-select")?.value || "";
  const url = profile ? `/v1/sessions?profile=${encodeURIComponent(profile)}` : "/v1/sessions";
  try {
    const body = await (await fetch(url)).json();
    list.innerHTML = "";
    for (const s of body.data || []) {
      const row = document.createElement("div");
      row.className = "session-row";
      const t = document.createElement("span");
      t.className = "sess-time";
      t.textContent = (s.created_at || "").slice(0, 16).replace("T", " ");
      const p = document.createElement("span");
      p.className = "sess-preview";
      p.textContent = s.preview || "(empty)";
      row.append(t, p);
      row.onclick = () => loadSession(s.id);
      list.appendChild(row);
    }
    if (!(body.data || []).length) list.innerHTML = '<p class="hint">No sessions yet.</p>';
  } catch (e) {
    list.innerHTML = '<p class="hint">Failed to load sessions.</p>';
  }
}

async function loadSession(id) {
  const body = await (await fetch(`/v1/sessions/${id}`)).json();
  currentSessionId = id;
  const dlg = el("chat-dialogue");
  dlg.innerHTML = "";
  for (const m of body.data.messages || []) {
    if (m.role !== "user" && m.role !== "assistant") continue;
    appendChatBubble(m.role, m.content); // reuse the existing dialogue-append helper
  }
  el("session-panel").classList.add("hidden");
}

if (el("session-list-btn")) el("session-list-btn").addEventListener("click", openSessionsPanel);
if (el("session-close-btn")) el("session-close-btn").addEventListener("click", () => el("session-panel").classList.add("hidden"));
if (el("session-new-btn")) el("session-new-btn").addEventListener("click", () => {
  currentSessionId = null;
  el("chat-dialogue").innerHTML = "";
});
```

`appendChatBubble` above is a placeholder NAME — find the actual function app.js uses to append a message to `#chat-dialogue` (search for `chat-dialogue` usages) and call that; if messages are appended inline, extract that snippet into a small helper first.

- [ ] **Step 6: Verify end-to-end**

Run the gateway (`make run` or the project's usual command; check `Makefile`), open `http://localhost:8000/static/index.html` and verify:
1. Profile panel shows Nickname + Memory section; adding/editing/deleting a memory hits the API (Network tab).
2. Text chat returns and reuses a `session_id` (second message, same id in the request query).
3. "Sessions" lists the session with a preview; clicking reloads the messages; "New session" clears.
4. Browser console free of errors.

Run: `.venv/bin/pytest tests/ -q` — all pass.

- [ ] **Step 7: Commit**

```bash
git add apps/api_gateway/app/static/index.html apps/api_gateway/app/static/app.js apps/api_gateway/app/static/styles.css
git commit -m "feat(ui): profile nickname + memory editor, sessions list with resume"
```

---

## Final verification

- [ ] `.venv/bin/pytest tests/ -q` — full suite green.
- [ ] `grep -rn "database_url" apps/` — setting used only in `db/engine.py`.
- [ ] `data/app.db` is gitignored: add `data/` to `.gitignore` if not present.
- [ ] Manual smoke: voice→voice session with a profile; after disconnect check `GET /v1/profiles/{name}/memories` gains extracted facts (needs a live LLM configured).
