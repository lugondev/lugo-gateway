# Knowledge Base Service (P1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `servers/knowledge-api` — a standalone service that stores documents in collections, chunks and embeds them, and answers semantic search queries — with nothing in `apps/` touched.

**Architecture:** FastAPI + SQLAlchemy 2.0 async over SQLite (Postgres via URL). A document upload persists raw metadata as `pending` and returns `202`; a background task extracts text, chunks it heading-aware, embeds in batches against an OpenAI-compatible `/embeddings` endpoint, and marks the document `indexed`. Search embeds the query, scores cosine against the collection's chunks, applies a `min_score` floor, and returns the top-k with their source heading. Every request is scoped by a tenant stamped from the bearer key, never read from the body.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 (async), aiosqlite, httpx, pydantic v2, pytest + pytest-asyncio, Docker.

## Global Constraints

- Package name `kbase`, CLI entry point `kb`, repo at `servers/knowledge-api`, its own git repository (`git init` in place; no remote, not registered as a submodule in this plan).
- `requires-python = ">=3.11"`. Develop against 3.12.
- Nothing under `apps/`, `tests/`, or any other existing directory is created or modified. The only paths this plan writes outside the new repo are this plan file itself.
- P1 accepts `.md`, `.txt`, and raw JSON text only. PDF and DOCX are P2 — do not add `pypdf` or `python-docx`.
- Tenant comes from the credential and overwrites anything in the request body.
- `KB_API_KEYS` unset means every authenticated route returns 401.
- Chunk sizing is measured in **characters**: `max_chars=800`, `overlap=100`.
- Default `min_score` is `0.35`; a search always applies a floor.
- All tests run from inside `servers/knowledge-api` with its own venv. Never run the parent repo's suite for this work.
- Commit inside the service repo with `git -c user.name=lugondev -c user.email=lugondev@gmail.com`.

## File Structure

```
servers/knowledge-api/
  pyproject.toml          packaging, deps, pytest config
  .gitignore              venv, __pycache__, *.db
  .env.example            every KB_* variable
  README.md               run it, configure it, call it
  Dockerfile              runtime image
  docker-compose.yml      service + optional postgres
  src/kbase/
    __init__.py
    errors.py             KbError, EmbeddingError, ExtractError
    settings.py           env -> Settings, plus check() for `kb doctor`
    types.py              pydantic wire types (requests + responses)
    db.py                 Database: engine, sessionmaker, create_all, dispose
    models.py             SQLAlchemy Collection / Document / Chunk
    chunker.py            pure text -> [ChunkPiece] with heading paths
    extract.py            bytes + mime -> text (P1: md/txt)
    embedding.py          batched OpenAI-compatible /embeddings client
    store.py              CRUD for collections, documents, chunks
    indexer.py            extract -> chunk -> embed -> finalize, with failure handling
    search.py             cosine, min_score floor, top-k
    cli.py                `kb doctor`, `kb serve`
    server/
      __init__.py
      app.py              create_app(), lifespan, app.state wiring
      auth.py             bearer -> tenant dependency
      routes.py           all HTTP endpoints
  tests/
    conftest.py           tmp-db app fixture, fake embedder, auth headers
    test_settings.py
    test_chunker.py
    test_auth.py
    test_collections.py
    test_documents.py
    test_indexer.py
    test_search.py
```

Boundary rule that decides the splits: `chunker.py`, `extract.py`, and `search.py` are **pure** — no database, no network, no settings. They are the parts whose correctness is subtle, and pure functions let their tests state the subtlety directly. Everything impure is concentrated in `store.py`, `embedding.py`, and `indexer.py`.

---

### Task 1: Repo scaffold, settings, and `kb doctor`

**Files:**
- Create: `servers/knowledge-api/pyproject.toml`
- Create: `servers/knowledge-api/.gitignore`
- Create: `servers/knowledge-api/src/kbase/__init__.py`
- Create: `servers/knowledge-api/src/kbase/errors.py`
- Create: `servers/knowledge-api/src/kbase/settings.py`
- Test: `servers/knowledge-api/tests/test_settings.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Settings` (frozen dataclass) with fields `api_keys: dict[str, str]`, `database_url: str`, `embed_base_url: str`, `embed_api_key: str`, `embed_model: str`, `max_upload_bytes: int`, `docs_enabled: bool`; classmethod `Settings.from_env(env: Mapping[str, str]) -> Settings`; method `Settings.check() -> list[str]` returning human-readable problems (empty list = healthy). Also `KbError`, `EmbeddingError`, `ExtractError` in `kbase.errors`.

- [ ] **Step 1: Create the repo and its own git history**

```bash
mkdir -p servers/knowledge-api/src/kbase/server servers/knowledge-api/tests
cd servers/knowledge-api
git init -q
```

Write `.gitignore`:

```
.venv/
__pycache__/
*.py[cod]
*.db
.pytest_cache/
.env
```

Write `pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "kbase"
version = "0.1.0"
description = "Documents in, retrievable chunks out"
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
  "pydantic>=2.7",
  "sqlalchemy[asyncio]>=2.0",
  "aiosqlite>=0.20",
  "httpx>=0.27",
  "fastapi>=0.115",
  "uvicorn[standard]>=0.30",
  "python-multipart>=0.0.9",
]

[project.scripts]
kb = "kbase.cli:main"

[project.optional-dependencies]
postgres = ["asyncpg>=0.29"]
dev = ["pytest>=8.3", "pytest-asyncio>=0.23", "ruff>=0.6"]

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

Then create the venv and install:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -q -e ".[dev]"
```

- [ ] **Step 2: Write the failing test**

`tests/test_settings.py`:

```python
from kbase.settings import Settings


def test_api_keys_parse_into_key_to_tenant():
    s = Settings.from_env({"KB_API_KEYS": "aaa:acme, bbb:globex"})
    assert s.api_keys == {"aaa": "acme", "bbb": "globex"}


def test_unset_api_keys_is_empty_not_a_wildcard():
    # An empty map is what makes every request a 401. It must never
    # degrade into "no keys configured means anyone may call".
    s = Settings.from_env({})
    assert s.api_keys == {}


def test_malformed_key_entry_is_ignored_not_guessed():
    s = Settings.from_env({"KB_API_KEYS": "nocolon, ccc:initech"})
    assert s.api_keys == {"ccc": "initech"}


def test_defaults():
    s = Settings.from_env({})
    assert s.database_url.startswith("sqlite+aiosqlite://")
    assert s.max_upload_bytes == 20_000_000
    assert s.docs_enabled is True


def test_docs_disabled_by_false_string():
    assert Settings.from_env({"KB_DOCS": "false"}).docs_enabled is False


def test_check_names_every_missing_requirement():
    problems = Settings.from_env({}).check()
    joined = " ".join(problems)
    assert "KB_API_KEYS" in joined
    assert "KB_EMBED_BASE_URL" in joined
    assert "KB_EMBED_MODEL" in joined


def test_check_passes_on_a_complete_environment():
    s = Settings.from_env({
        "KB_API_KEYS": "aaa:acme",
        "KB_EMBED_BASE_URL": "http://localhost:1234/v1",
        "KB_EMBED_MODEL": "text-embedding-3-small",
    })
    assert s.check() == []
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_settings.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kbase.settings'`

- [ ] **Step 4: Write the implementation**

`src/kbase/__init__.py`: empty file.

`src/kbase/errors.py`:

```python
"""Every failure this service raises on purpose."""

from __future__ import annotations


class KbError(Exception):
    """Base for anything kbase raises deliberately."""


class EmbeddingError(KbError):
    """The embedding provider refused, timed out, or answered nonsense."""


class ExtractError(KbError):
    """A document's bytes could not be turned into text."""
```

`src/kbase/settings.py`:

```python
"""Configuration, and the checks `kb doctor` runs before anything depends on it."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./kbase.db"
DEFAULT_MAX_UPLOAD_BYTES = 20_000_000


def _parse_api_keys(raw: str) -> dict[str, str]:
    """`key:tenant,key:tenant` -> {key: tenant}.

    An entry without a colon is dropped rather than guessed at. Treating a bare
    string as "a key with some default tenant" is how one tenant ends up reading
    another's collections, so a malformed entry simply does not grant access.
    """
    out: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        key, _, tenant = entry.partition(":")
        key, tenant = key.strip(), tenant.strip()
        if key and tenant:
            out[key] = tenant
    return out


@dataclass(frozen=True)
class Settings:
    api_keys: dict[str, str] = field(default_factory=dict)
    database_url: str = DEFAULT_DATABASE_URL
    embed_base_url: str = ""
    embed_api_key: str = ""
    embed_model: str = ""
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    docs_enabled: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        raw_max = env.get("KB_MAX_UPLOAD_BYTES", "").strip()
        try:
            max_upload = int(raw_max) if raw_max else DEFAULT_MAX_UPLOAD_BYTES
        except ValueError:
            max_upload = DEFAULT_MAX_UPLOAD_BYTES
        return cls(
            api_keys=_parse_api_keys(env.get("KB_API_KEYS", "")),
            database_url=env.get("KB_DATABASE_URL", "").strip() or DEFAULT_DATABASE_URL,
            embed_base_url=env.get("KB_EMBED_BASE_URL", "").strip(),
            embed_api_key=env.get("KB_EMBED_API_KEY", "").strip(),
            embed_model=env.get("KB_EMBED_MODEL", "").strip(),
            max_upload_bytes=max_upload,
            docs_enabled=env.get("KB_DOCS", "").strip().lower() not in {"false", "0", "no"},
        )

    def check(self) -> list[str]:
        """Everything wrong that can be known without making a request."""
        problems: list[str] = []
        if not self.api_keys:
            problems.append("KB_API_KEYS is unset: every request will be rejected with 401")
        if not self.embed_base_url:
            problems.append("KB_EMBED_BASE_URL is unset: nothing can be indexed or searched")
        if not self.embed_model:
            problems.append("KB_EMBED_MODEL is unset: nothing can be indexed or searched")
        if self.max_upload_bytes <= 0:
            problems.append("KB_MAX_UPLOAD_BYTES must be a positive integer")
        return problems
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_settings.py -v`
Expected: PASS (7 tests)

- [ ] **Step 6: Commit**

```bash
git add .gitignore pyproject.toml src tests
git -c user.name=lugondev -c user.email=lugondev@gmail.com \
  commit -q -m "feat(settings): environment config and the checks doctor runs"
```

---

### Task 2: Database models and the collection store

**Files:**
- Create: `servers/knowledge-api/src/kbase/db.py`
- Create: `servers/knowledge-api/src/kbase/models.py`
- Create: `servers/knowledge-api/src/kbase/store.py`
- Test: `servers/knowledge-api/tests/test_collections.py`

**Interfaces:**
- Consumes: `Settings` (Task 1).
- Produces:
  - `Database(url: str)` with `async create_all() -> None`, `session() -> AsyncSession` async context manager, `async dispose() -> None`.
  - `models.Collection`, `models.Document`, `models.Chunk` (SQLAlchemy).
  - `store.CollectionStore(db)` with `async create(tenant, name) -> dict`, `async list(tenant) -> list[dict]`, `async get(tenant, name) -> dict | None`, `async resolve_id(tenant, name) -> str | None`, `async delete(tenant, name) -> bool`.
  - A collection dict is `{"name": str, "document_count": int}`.

- [ ] **Step 1: Write the failing test**

`tests/test_collections.py`:

```python
import pytest

from kbase.db import Database
from kbase.store import CollectionStore


@pytest.fixture
async def db(tmp_path):
    d = Database(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await d.create_all()
    yield d
    await d.dispose()


async def test_create_then_list(db):
    store = CollectionStore(db)
    await store.create("acme", "faq")
    assert await store.list("acme") == [{"name": "faq", "document_count": 0}]


async def test_same_name_under_two_tenants_are_two_rows(db):
    # The whole point of keeping tenant a separate dimension: `faq` belonging to
    # acme and `faq` belonging to globex must not be the same collection.
    store = CollectionStore(db)
    await store.create("acme", "faq")
    await store.create("globex", "faq")
    assert await store.list("acme") == [{"name": "faq", "document_count": 0}]
    assert await store.list("globex") == [{"name": "faq", "document_count": 0}]


async def test_create_is_idempotent_within_a_tenant(db):
    store = CollectionStore(db)
    first = await store.create("acme", "faq")
    again = await store.create("acme", "faq")
    assert first == again
    assert len(await store.list("acme")) == 1


async def test_get_is_scoped_to_the_tenant(db):
    store = CollectionStore(db)
    await store.create("acme", "faq")
    assert await store.get("acme", "faq") is not None
    assert await store.get("globex", "faq") is None


async def test_delete_reports_whether_anything_was_deleted(db):
    store = CollectionStore(db)
    await store.create("acme", "faq")
    assert await store.delete("acme", "faq") is True
    assert await store.delete("acme", "faq") is False
    assert await store.list("acme") == []


async def test_delete_is_scoped_to_the_tenant(db):
    store = CollectionStore(db)
    await store.create("acme", "faq")
    assert await store.delete("globex", "faq") is False
    assert await store.get("acme", "faq") is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_collections.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kbase.db'`

- [ ] **Step 3: Write the implementation**

`src/kbase/db.py`:

```python
"""One engine, one sessionmaker, and a context manager that always closes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from kbase.models import Base


class Database:
    def __init__(self, url: str) -> None:
        self._engine = create_async_engine(url, future=True)
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)

    async def create_all(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._sessionmaker() as s:
            yield s

    async def dispose(self) -> None:
        await self._engine.dispose()
```

`src/kbase/models.py`:

```python
"""The three tables. Deletes cascade in `store.py`, not in the schema.

SQLite enforces ON DELETE CASCADE only when `PRAGMA foreign_keys=ON` is set on
every connection, and a pooled async engine makes that easy to get wrong in one
place and not another. Deleting children explicitly behaves identically on SQLite
and Postgres, which is worth more here than the brevity.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Collection(Base):
    __tablename__ = "collections"
    __table_args__ = (UniqueConstraint("tenant", "name", name="uq_collection_tenant_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("collection_id", "sha256", name="uq_document_collection_sha"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    collection_id: Mapped[str] = mapped_column(ForeignKey("collections.id"), index=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    filename: Mapped[str] = mapped_column(String(512), default="")
    mime: Mapped[str] = mapped_column(String(128), default="")
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    bytes_len: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    heading: Mapped[str] = mapped_column(String(512), default="")
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    embedding: Mapped[list[float]] = mapped_column(JSON, default=list)
```

`src/kbase/store.py`:

```python
"""Every read and write, each one scoped by tenant at the query, never after it."""

from __future__ import annotations

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select

from kbase.db import Database
from kbase.models import Collection, Document


class CollectionStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, tenant: str, name: str) -> dict:
        """Idempotent: creating an existing collection returns it untouched."""
        async with self._db.session() as s:
            row = (
                await s.execute(
                    select(Collection).where(
                        Collection.tenant == tenant, Collection.name == name
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = Collection(tenant=tenant, name=name)
                s.add(row)
                await s.commit()
            return {"name": row.name, "document_count": await self._count(s, row.id)}

    async def list(self, tenant: str) -> list[dict]:
        async with self._db.session() as s:
            rows = (
                await s.execute(
                    select(Collection)
                    .where(Collection.tenant == tenant)
                    .order_by(Collection.name)
                )
            ).scalars().all()
            return [
                {"name": r.name, "document_count": await self._count(s, r.id)} for r in rows
            ]

    async def get(self, tenant: str, name: str) -> dict | None:
        async with self._db.session() as s:
            row = await self._row(s, tenant, name)
            if row is None:
                return None
            return {"name": row.name, "document_count": await self._count(s, row.id)}

    async def resolve_id(self, tenant: str, name: str) -> str | None:
        """The internal id, for callers that need to hang documents off it."""
        async with self._db.session() as s:
            row = await self._row(s, tenant, name)
            return row.id if row else None

    async def delete(self, tenant: str, name: str) -> bool:
        async with self._db.session() as s:
            row = await self._row(s, tenant, name)
            if row is None:
                return False
            doc_ids = (
                await s.execute(
                    select(Document.id).where(Document.collection_id == row.id)
                )
            ).scalars().all()
            if doc_ids:
                from kbase.models import Chunk

                await s.execute(sa_delete(Chunk).where(Chunk.document_id.in_(doc_ids)))
                await s.execute(sa_delete(Document).where(Document.id.in_(doc_ids)))
            await s.delete(row)
            await s.commit()
            return True

    @staticmethod
    async def _row(s, tenant: str, name: str) -> Collection | None:
        return (
            await s.execute(
                select(Collection).where(Collection.tenant == tenant, Collection.name == name)
            )
        ).scalar_one_or_none()

    @staticmethod
    async def _count(s, collection_id: str) -> int:
        return int(
            (
                await s.execute(
                    select(func.count())
                    .select_from(Document)
                    .where(Document.collection_id == collection_id)
                )
            ).scalar_one()
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_collections.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/kbase/db.py src/kbase/models.py src/kbase/store.py tests/test_collections.py
git -c user.name=lugondev -c user.email=lugondev@gmail.com \
  commit -q -m "feat(store): collections, scoped by tenant at every query"
```

---

### Task 3: The chunker

**Files:**
- Create: `servers/knowledge-api/src/kbase/chunker.py`
- Test: `servers/knowledge-api/tests/test_chunker.py`

**Interfaces:**
- Consumes: nothing (pure module).
- Produces: `ChunkPiece` (frozen dataclass with `text: str`, `heading: str`, `ordinal: int`) and `chunk_markdown(text: str, *, max_chars: int = 800, overlap: int = 100) -> list[ChunkPiece]`.

- [ ] **Step 1: Write the failing test**

`tests/test_chunker.py`:

```python
from kbase.chunker import chunk_markdown


def test_empty_input_produces_nothing():
    assert chunk_markdown("") == []
    assert chunk_markdown("   \n\n  ") == []


def test_short_document_is_one_chunk():
    pieces = chunk_markdown("Bảo hành 12 tháng kể từ ngày mua.")
    assert len(pieces) == 1
    assert pieces[0].text == "Bảo hành 12 tháng kể từ ngày mua."
    assert pieces[0].ordinal == 0


def test_heading_path_is_carried_on_each_piece():
    text = "# Sổ tay\n\nMở đầu.\n\n## Bảo hành\n\nMười hai tháng.\n\n### Đổi trả\n\nBảy ngày.\n"
    pieces = chunk_markdown(text)
    by_text = {p.text.strip(): p.heading for p in pieces}
    assert by_text["Mở đầu."] == "Sổ tay"
    assert by_text["Mười hai tháng."] == "Sổ tay > Bảo hành"
    assert by_text["Bảy ngày."] == "Sổ tay > Bảo hành > Đổi trả"


def test_deeper_heading_replaces_only_its_own_level():
    text = "## A\n\naaa\n\n### A1\n\nbbb\n\n## B\n\nccc\n"
    pieces = chunk_markdown(text)
    by_text = {p.text.strip(): p.heading for p in pieces}
    assert by_text["bbb"] == "A > A1"
    assert by_text["ccc"] == "B"


def test_long_section_splits_with_overlap():
    body = ". ".join(f"Câu số {i}" for i in range(400)) + "."
    pieces = chunk_markdown(body, max_chars=200, overlap=50)
    assert len(pieces) > 1
    assert all(len(p.text) <= 200 for p in pieces)
    # Overlap means consecutive pieces share text; without it a sentence cut in
    # half at a boundary is retrievable from neither side.
    assert pieces[0].text[-20:] in pieces[1].text


def test_ordinals_are_contiguous_across_the_whole_document():
    text = "## A\n\n" + ("x" * 500) + "\n\n## B\n\n" + ("y" * 500)
    pieces = chunk_markdown(text, max_chars=200, overlap=50)
    assert [p.ordinal for p in pieces] == list(range(len(pieces)))


def test_one_enormous_line_terminates():
    # No sentence boundary anywhere: the splitter must still make progress
    # rather than re-cutting the same window forever.
    pieces = chunk_markdown("x" * 50_000, max_chars=800, overlap=100)
    assert len(pieces) > 1
    assert all(len(p.text) <= 800 for p in pieces)
    assert sum(len(p.text) for p in pieces) < 200_000


def test_overlap_larger_than_max_chars_still_terminates():
    pieces = chunk_markdown("y" * 5_000, max_chars=100, overlap=500)
    assert len(pieces) > 1


def test_heading_with_no_body_produces_no_chunk():
    assert chunk_markdown("## Trống\n\n## Cũng trống\n") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_chunker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kbase.chunker'`

- [ ] **Step 3: Write the implementation**

`src/kbase/chunker.py`:

```python
"""Text -> retrievable pieces, cut on headings first and length second.

Sizes are in characters, not tokens. Every embedding provider tokenises
differently, and Vietnamese diacritics make token estimates swing hard enough
that a "400 token" chunk can be half the length one provider to the next.
Characters behave identically everywhere, and the only thing the limit really
has to do is keep a chunk small enough to be about one idea.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_BOUNDARY = re.compile(r"[.!?…]\s|\n")


@dataclass(frozen=True)
class ChunkPiece:
    text: str
    heading: str
    ordinal: int


def _sections(text: str) -> list[tuple[str, str]]:
    """[(heading path, body)] in document order."""
    stack: list[str] = []
    out: list[tuple[str, str]] = []
    body: list[str] = []

    def flush() -> None:
        joined = "\n".join(body).strip()
        if joined:
            out.append((" > ".join(stack), joined))
        body.clear()

    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            flush()
            level = len(m.group(1))
            del stack[level - 1 :]
            stack.append(m.group(2))
        else:
            body.append(line)
    flush()
    return out


def _split_body(body: str, max_chars: int, overlap: int) -> list[str]:
    if len(body) <= max_chars:
        return [body]
    # Overlap must leave room to advance; otherwise every window starts where the
    # previous one did and the loop never ends.
    step_back = min(overlap, max_chars // 2)
    out: list[str] = []
    start = 0
    while start < len(body):
        end = min(start + max_chars, len(body))
        if end < len(body):
            window = body[start:end]
            cut = -1
            for m in _BOUNDARY.finditer(window):
                if m.end() > max_chars - step_back:
                    cut = m.end()
                    break
            if cut > 0:
                end = start + cut
        piece = body[start:end].strip()
        if piece:
            out.append(piece)
        if end >= len(body):
            break
        start = max(end - step_back, start + 1)
    return out


def chunk_markdown(
    text: str, *, max_chars: int = 800, overlap: int = 100
) -> list[ChunkPiece]:
    pieces: list[ChunkPiece] = []
    for heading, body in _sections(text):
        for piece in _split_body(body, max_chars, overlap):
            pieces.append(ChunkPiece(text=piece, heading=heading, ordinal=len(pieces)))
    return pieces
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_chunker.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add src/kbase/chunker.py tests/test_chunker.py
git -c user.name=lugondev -c user.email=lugondev@gmail.com \
  commit -q -m "feat(chunker): heading-aware splitting that always makes progress"
```

---

### Task 4: Text extraction and the embedding client

**Files:**
- Create: `servers/knowledge-api/src/kbase/extract.py`
- Create: `servers/knowledge-api/src/kbase/embedding.py`
- Test: `servers/knowledge-api/tests/test_extract_embed.py`

**Interfaces:**
- Consumes: `ExtractError`, `EmbeddingError` (Task 1).
- Produces:
  - `extract.extract_text(data: bytes, *, filename: str = "", mime: str = "") -> str` — raises `ExtractError` on anything it cannot decode or does not support.
  - `embedding.Embedder = Callable[[list[str]], Awaitable[tuple[list[list[float]], int]]]` (type alias).
  - `embedding.make_embedder(*, base_url: str, api_key: str, model: str, batch_size: int = 32, timeout: float = 60.0, transport: httpx.AsyncBaseTransport | None = None) -> Embedder` — returns an async callable that yields `(vectors, prompt_tokens)`. `transport` exists so tests can drive it with `httpx.MockTransport` instead of a network.

- [ ] **Step 1: Write the failing test**

`tests/test_extract_embed.py`:

```python
import httpx
import pytest

from kbase.embedding import make_embedder
from kbase.errors import EmbeddingError, ExtractError
from kbase.extract import extract_text


def test_utf8_text_decodes():
    assert extract_text("Bảo hành".encode(), filename="a.txt") == "Bảo hành"


def test_markdown_decodes():
    assert extract_text(b"# Title\n\nbody", filename="a.md").startswith("# Title")


def test_undecodable_bytes_raise_extract_error():
    with pytest.raises(ExtractError):
        extract_text(b"\xff\xfe\x00\x00\xff", filename="a.txt")


def test_unsupported_type_names_itself():
    with pytest.raises(ExtractError) as exc:
        extract_text(b"%PDF-1.4 ...", filename="manual.pdf")
    assert "pdf" in str(exc.value).lower()


def _transport(handler):
    return httpx.MockTransport(handler)


async def test_embedder_batches_and_sums_tokens():
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.read().decode()
        import json

        body = json.loads(payload)
        seen.append(len(body["input"]))
        return httpx.Response(
            200,
            json={
                "data": [{"embedding": [0.1, 0.2]} for _ in body["input"]],
                "usage": {"prompt_tokens": 7},
            },
        )

    embed = make_embedder(
        base_url="http://x/v1", api_key="k", model="m", batch_size=2,
        transport=_transport(handler),
    )
    vectors, tokens = await embed(["a", "b", "c"])
    assert seen == [2, 1]          # batched, not one giant request
    assert len(vectors) == 3
    assert tokens == 14            # summed across batches, not taken from the last


async def test_embedder_preserves_input_order():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.read().decode())
        return httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [float(len(t))]} for t in body["input"]
                ],
                "usage": {"prompt_tokens": 1},
            },
        )

    embed = make_embedder(
        base_url="http://x/v1", api_key="k", model="m", batch_size=2,
        transport=_transport(handler),
    )
    vectors, _ = await embed(["a", "bb", "ccc", "dddd"])
    assert vectors == [[1.0], [2.0], [3.0], [4.0]]


async def test_provider_error_becomes_embedding_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "slow down"})

    embed = make_embedder(
        base_url="http://x/v1", api_key="k", model="m", transport=_transport(handler)
    )
    with pytest.raises(EmbeddingError):
        await embed(["a"])


async def test_wrong_vector_count_is_an_error_not_a_silent_truncation():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": [{"embedding": [0.1]}], "usage": {"prompt_tokens": 1}}
        )

    embed = make_embedder(
        base_url="http://x/v1", api_key="k", model="m", batch_size=8,
        transport=_transport(handler),
    )
    with pytest.raises(EmbeddingError):
        await embed(["a", "b"])


async def test_empty_input_makes_no_request():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not have been called")

    embed = make_embedder(
        base_url="http://x/v1", api_key="k", model="m", transport=_transport(handler)
    )
    assert await embed([]) == ([], 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_extract_embed.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kbase.embedding'`

- [ ] **Step 3: Write the implementation**

`src/kbase/extract.py`:

```python
"""Bytes -> text. P1 handles what decodes; PDF and DOCX arrive in P2."""

from __future__ import annotations

from kbase.errors import ExtractError

_TEXT_SUFFIXES = (".txt", ".md", ".markdown", "")
_KNOWN_BINARY = {".pdf": "pdf", ".docx": "docx", ".doc": "doc"}


def _suffix(filename: str) -> str:
    if "." not in filename:
        return ""
    return "." + filename.rsplit(".", 1)[1].lower()


def extract_text(data: bytes, *, filename: str = "", mime: str = "") -> str:
    suffix = _suffix(filename)
    if suffix in _KNOWN_BINARY:
        raise ExtractError(
            f"{_KNOWN_BINARY[suffix]} files are not supported yet (planned for P2)"
        )
    if suffix and suffix not in _TEXT_SUFFIXES and not mime.startswith("text/"):
        raise ExtractError(f"unsupported file type: {suffix}")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExtractError("file is not valid UTF-8 text") from exc
```

`src/kbase/embedding.py`:

```python
"""The embedding call, batched, order-preserving, and loud about mismatches."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx

from kbase.errors import EmbeddingError

Embedder = Callable[[list[str]], Awaitable[tuple[list[list[float]], int]]]

DEFAULT_TIMEOUT = 60.0


def make_embedder(
    *,
    base_url: str,
    api_key: str,
    model: str,
    batch_size: int = 32,
    timeout: float = DEFAULT_TIMEOUT,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Embedder:
    url = f"{base_url.rstrip('/')}/embeddings"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def embed(texts: list[str]) -> tuple[list[list[float]], int]:
        if not texts:
            return [], 0
        vectors: list[list[float]] = []
        tokens = 0
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            for start in range(0, len(texts), batch_size):
                batch = texts[start : start + batch_size]
                try:
                    resp = await client.post(
                        url, headers=headers, json={"model": model, "input": batch}
                    )
                    resp.raise_for_status()
                    body = resp.json()
                except (httpx.HTTPError, ValueError) as exc:
                    raise EmbeddingError(f"embedding request failed: {exc}") from exc
                data = body.get("data") or []
                if len(data) != len(batch):
                    # Zipping a short reply onto the batch would attach one chunk's
                    # vector to a different chunk's text, and every later search
                    # would be quietly wrong with no error anywhere.
                    raise EmbeddingError(
                        f"embedding provider returned {len(data)} vectors for {len(batch)} inputs"
                    )
                vectors.extend(d["embedding"] for d in data)
                tokens += int((body.get("usage") or {}).get("prompt_tokens") or 0)
        return vectors, tokens

    return embed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_extract_embed.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add src/kbase/extract.py src/kbase/embedding.py tests/test_extract_embed.py
git -c user.name=lugondev -c user.email=lugondev@gmail.com \
  commit -q -m "feat(embed): batched embedding client and P1 text extraction"
```

---

### Task 5: The indexer

**Files:**
- Create: `servers/knowledge-api/src/kbase/indexer.py`
- Modify: `servers/knowledge-api/src/kbase/store.py` (add `DocumentStore`)
- Test: `servers/knowledge-api/tests/test_indexer.py`

**Interfaces:**
- Consumes: `Database`, `CollectionStore` (Task 2), `chunk_markdown`/`ChunkPiece` (Task 3), `extract_text`, `Embedder` (Task 4).
- Produces:
  - `store.DocumentStore(db)` with `async create(collection_id, *, title, filename, mime, sha256, data: bytes) -> tuple[dict, bool]` (the bool is `created`; `False` means an identical sha256 already existed), `async get(document_id) -> dict | None`, `async owner_collection_id(document_id) -> str | None`, `async list(collection_id, status: str | None = None) -> list[dict]`, `async delete(document_id) -> bool`, `async raw_bytes(document_id) -> bytes | None`, `async chunks(document_id) -> list[dict]` (each `{"id", "ordinal", "text", "heading", "embedding"}`), `async mark_indexed(document_id, chunk_count) -> None`, `async mark_failed(document_id, reason) -> None`, `async replace_chunks(document_id, rows: list[dict]) -> None`, `async drop_chunks(document_id) -> None`.
  - A document dict is `{"id", "title", "filename", "mime", "sha256", "bytes_len", "status", "error", "chunk_count", "created_at", "indexed_at"}` with ISO-8601 strings for the timestamps (`indexed_at` may be `None`).
  - `indexer.index_document(db: Database, document_id: str, *, embed: Embedder, max_chars: int = 800, overlap: int = 100) -> None` — never raises; failures land in the document's `status`/`error`.

Note: `Document` gains a `data` column (`LargeBinary`) so a re-index does not require the client to upload again. Add it to `models.Document` in this task:

```python
    data: Mapped[bytes] = mapped_column(LargeBinary, default=b"")
```
with `LargeBinary` added to the `sqlalchemy` import line in `models.py`.

- [ ] **Step 1: Write the failing test**

`tests/test_indexer.py`:

```python
import hashlib

import pytest

from kbase.db import Database
from kbase.errors import EmbeddingError
from kbase.indexer import index_document
from kbase.store import CollectionStore, DocumentStore


@pytest.fixture
async def db(tmp_path):
    d = Database(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await d.create_all()
    yield d
    await d.dispose()


@pytest.fixture
async def collection_id(db):
    store = CollectionStore(db)
    await store.create("acme", "faq")
    return await store.resolve_id("acme", "faq")


def fake_embedder(dim: int = 3):
    async def embed(texts):
        return [[float(len(t) % 7), 1.0, 0.0] for t in texts], len(texts)

    return embed


def failing_embedder():
    async def embed(texts):
        raise EmbeddingError("provider said no")

    return embed


def half_failing_embedder():
    """Succeeds on the first batch, fails on the second."""
    calls = {"n": 0}

    async def embed(texts):
        calls["n"] += 1
        if calls["n"] > 1:
            raise EmbeddingError("provider said no")
        return [[1.0, 0.0, 0.0] for _ in texts], len(texts)

    return embed


async def _add(db, collection_id, text: str, filename="a.md"):
    data = text.encode()
    docs = DocumentStore(db)
    doc, _created = await docs.create(
        collection_id,
        title="t",
        filename=filename,
        mime="text/markdown",
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    )
    return doc["id"]


async def test_happy_path_marks_indexed_with_chunk_count(db, collection_id):
    doc_id = await _add(db, collection_id, "## Bảo hành\n\nMười hai tháng.\n")
    await index_document(db, doc_id, embed=fake_embedder())
    doc = await DocumentStore(db).get(doc_id)
    assert doc["status"] == "indexed"
    assert doc["chunk_count"] == 1
    assert doc["error"] == ""
    assert doc["indexed_at"] is not None


async def test_chunks_carry_heading_and_embedding(db, collection_id):
    doc_id = await _add(db, collection_id, "## Bảo hành\n\nMười hai tháng.\n")
    await index_document(db, doc_id, embed=fake_embedder())
    rows = await DocumentStore(db).chunks(doc_id)
    assert rows[0]["heading"] == "Bảo hành"
    assert len(rows[0]["embedding"]) == 3


async def test_embedding_failure_marks_failed_and_leaves_no_chunks(db, collection_id):
    doc_id = await _add(db, collection_id, "## A\n\nbody\n")
    await index_document(db, doc_id, embed=failing_embedder())
    docs = DocumentStore(db)
    doc = await docs.get(doc_id)
    assert doc["status"] == "failed"
    assert "provider said no" in doc["error"]
    assert doc["chunk_count"] == 0
    assert await docs.chunks(doc_id) == []


async def test_failure_midway_leaves_no_partial_index(db, collection_id):
    # A document that reports `indexed` while holding only the first third of the
    # manual answers questions from that third and never says it is incomplete.
    body = "\n\n".join(f"Đoạn {i} " + "x" * 700 for i in range(80))
    doc_id = await _add(db, collection_id, body)
    await index_document(db, doc_id, embed=half_failing_embedder())
    docs = DocumentStore(db)
    doc = await docs.get(doc_id)
    assert doc["status"] == "failed"
    assert await docs.chunks(doc_id) == []


async def test_unsupported_file_fails_that_document_only(db, collection_id):
    bad = await _add(db, collection_id, "irrelevant", filename="manual.pdf")
    good = await _add(db, collection_id, "## A\n\nbody\n", filename="ok.md")
    await index_document(db, bad, embed=fake_embedder())
    await index_document(db, good, embed=fake_embedder())
    docs = DocumentStore(db)
    assert (await docs.get(bad))["status"] == "failed"
    assert (await docs.get(good))["status"] == "indexed"


async def test_empty_document_indexes_with_zero_chunks(db, collection_id):
    doc_id = await _add(db, collection_id, "   \n\n  ")
    await index_document(db, doc_id, embed=fake_embedder())
    doc = await DocumentStore(db).get(doc_id)
    assert doc["status"] == "indexed"
    assert doc["chunk_count"] == 0


async def test_reindex_replaces_rather_than_appends(db, collection_id):
    doc_id = await _add(db, collection_id, "## A\n\nbody\n")
    await index_document(db, doc_id, embed=fake_embedder())
    await index_document(db, doc_id, embed=fake_embedder())
    docs = DocumentStore(db)
    assert (await docs.get(doc_id))["chunk_count"] == 1
    assert len(await docs.chunks(doc_id)) == 1


async def test_missing_document_is_a_no_op_not_a_crash(db):
    await index_document(db, "does-not-exist", embed=fake_embedder())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_indexer.py -v`
Expected: FAIL — `ImportError: cannot import name 'DocumentStore' from 'kbase.store'`

- [ ] **Step 3: Add `DocumentStore` to `store.py`**

Append to `src/kbase/store.py` (and extend the imports at the top to
`from kbase.models import Chunk, Collection, Document` and add
`from kbase.models import utcnow`):

```python
def _doc_dict(d: Document) -> dict:
    return {
        "id": d.id,
        "title": d.title,
        "filename": d.filename,
        "mime": d.mime,
        "sha256": d.sha256,
        "bytes_len": d.bytes_len,
        "status": d.status,
        "error": d.error,
        "chunk_count": d.chunk_count,
        "created_at": d.created_at.isoformat(),
        "indexed_at": d.indexed_at.isoformat() if d.indexed_at else None,
    }


class DocumentStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(
        self,
        collection_id: str,
        *,
        title: str,
        filename: str,
        mime: str,
        sha256: str,
        data: bytes,
    ) -> tuple[dict, bool]:
        """Returns (document, created). `created is False` means these exact bytes
        were already in this collection -- a retried upload, not a second copy."""
        async with self._db.session() as s:
            existing = (
                await s.execute(
                    select(Document).where(
                        Document.collection_id == collection_id,
                        Document.sha256 == sha256,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return _doc_dict(existing), False
            row = Document(
                collection_id=collection_id,
                title=title,
                filename=filename,
                mime=mime,
                sha256=sha256,
                bytes_len=len(data),
                data=data,
                status="pending",
            )
            s.add(row)
            await s.commit()
            return _doc_dict(row), True

    async def get(self, document_id: str) -> dict | None:
        async with self._db.session() as s:
            row = await s.get(Document, document_id)
            return _doc_dict(row) if row else None

    async def owner_collection_id(self, document_id: str) -> str | None:
        async with self._db.session() as s:
            row = await s.get(Document, document_id)
            return row.collection_id if row else None

    async def raw_bytes(self, document_id: str) -> bytes | None:
        async with self._db.session() as s:
            row = await s.get(Document, document_id)
            return bytes(row.data) if row else None

    async def list(self, collection_id: str, status: str | None = None) -> list[dict]:
        async with self._db.session() as s:
            stmt = select(Document).where(Document.collection_id == collection_id)
            if status:
                stmt = stmt.where(Document.status == status)
            rows = (
                await s.execute(stmt.order_by(Document.created_at.desc(), Document.id))
            ).scalars().all()
            return [_doc_dict(r) for r in rows]

    async def delete(self, document_id: str) -> bool:
        async with self._db.session() as s:
            row = await s.get(Document, document_id)
            if row is None:
                return False
            await s.execute(sa_delete(Chunk).where(Chunk.document_id == document_id))
            await s.delete(row)
            await s.commit()
            return True

    async def chunks(self, document_id: str) -> list[dict]:
        async with self._db.session() as s:
            rows = (
                await s.execute(
                    select(Chunk)
                    .where(Chunk.document_id == document_id)
                    .order_by(Chunk.ordinal)
                )
            ).scalars().all()
            return [
                {
                    "id": c.id,
                    "ordinal": c.ordinal,
                    "text": c.text,
                    "heading": c.heading,
                    "embedding": list(c.embedding or []),
                }
                for c in rows
            ]

    async def drop_chunks(self, document_id: str) -> None:
        async with self._db.session() as s:
            await s.execute(sa_delete(Chunk).where(Chunk.document_id == document_id))
            await s.commit()

    async def replace_chunks(self, document_id: str, rows: list[dict]) -> None:
        async with self._db.session() as s:
            await s.execute(sa_delete(Chunk).where(Chunk.document_id == document_id))
            for r in rows:
                s.add(
                    Chunk(
                        document_id=document_id,
                        ordinal=r["ordinal"],
                        text=r["text"],
                        heading=r["heading"],
                        char_count=len(r["text"]),
                        embedding=r["embedding"],
                    )
                )
            await s.commit()

    async def mark_indexed(self, document_id: str, chunk_count: int) -> None:
        async with self._db.session() as s:
            row = await s.get(Document, document_id)
            if row is None:
                return
            row.status = "indexed"
            row.error = ""
            row.chunk_count = chunk_count
            row.indexed_at = utcnow()
            await s.commit()

    async def mark_failed(self, document_id: str, reason: str) -> None:
        async with self._db.session() as s:
            row = await s.get(Document, document_id)
            if row is None:
                return
            row.status = "failed"
            row.error = reason[:2000]
            row.chunk_count = 0
            row.indexed_at = None
            await s.commit()
```

- [ ] **Step 4: Write the indexer**

`src/kbase/indexer.py`:

```python
"""extract -> chunk -> embed -> finalize, and never in a half state.

Chunks are written in one shot at the end rather than batch by batch. Writing as
each batch returns would leave a document that failed on batch 7 holding the
first six batches' chunks; those chunks are indistinguishable from a complete
document at search time, so the assistant answers from the first third of the
manual and never mentions that the rest is missing.
"""

from __future__ import annotations

import logging

from kbase.chunker import chunk_markdown
from kbase.db import Database
from kbase.embedding import Embedder
from kbase.errors import KbError
from kbase.extract import extract_text
from kbase.store import DocumentStore

logger = logging.getLogger(__name__)


async def index_document(
    db: Database,
    document_id: str,
    *,
    embed: Embedder,
    max_chars: int = 800,
    overlap: int = 100,
) -> None:
    """Index one document. Never raises: a failure is recorded on the row."""
    docs = DocumentStore(db)
    doc = await docs.get(document_id)
    if doc is None:
        logger.warning("index requested for unknown document %s", document_id)
        return
    try:
        data = await docs.raw_bytes(document_id) or b""
        text = extract_text(data, filename=doc["filename"], mime=doc["mime"])
        pieces = chunk_markdown(text, max_chars=max_chars, overlap=overlap)
        if not pieces:
            await docs.drop_chunks(document_id)
            await docs.mark_indexed(document_id, 0)
            return
        vectors, _tokens = await embed([p.text for p in pieces])
        if len(vectors) != len(pieces):
            raise KbError(
                f"embedder returned {len(vectors)} vectors for {len(pieces)} chunks"
            )
        await docs.replace_chunks(
            document_id,
            [
                {
                    "ordinal": p.ordinal,
                    "text": p.text,
                    "heading": p.heading,
                    "embedding": v,
                }
                for p, v in zip(pieces, vectors, strict=True)
            ],
        )
        await docs.mark_indexed(document_id, len(pieces))
    except Exception as exc:  # noqa: BLE001 - the failure belongs on the row
        logger.warning("indexing document %s failed: %s", document_id, exc)
        await docs.drop_chunks(document_id)
        await docs.mark_failed(document_id, str(exc))
```

Also add `LargeBinary` to the `sqlalchemy` import in `models.py` and the `data` column to `Document` as described in the Interfaces block.

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_indexer.py -v`
Expected: PASS (8 tests)

- [ ] **Step 6: Run the whole suite**

Run: `.venv/bin/pytest -q`
Expected: PASS, no regressions in Tasks 1–4.

- [ ] **Step 7: Commit**

```bash
git add src/kbase/indexer.py src/kbase/store.py src/kbase/models.py tests/test_indexer.py
git -c user.name=lugondev -c user.email=lugondev@gmail.com \
  commit -q -m "feat(indexer): index a document whole, or record why it failed"
```

---

### Task 6: Search

**Files:**
- Create: `servers/knowledge-api/src/kbase/search.py`
- Test: `servers/knowledge-api/tests/test_search.py`

**Interfaces:**
- Consumes: `Database` (Task 2), `Embedder` (Task 4), `models.Chunk`/`Document` (Task 2).
- Produces:
  - `search.cosine(a: list[float], b: list[float]) -> float`
  - `search.search_collection(db, collection_id: str, query: str, *, embed: Embedder, limit: int = 5, min_score: float = 0.35) -> tuple[list[dict], int]` — returns `(hits, prompt_tokens)`. A hit is `{"text", "score", "document_id", "title", "filename", "heading"}`, sorted by descending score.

- [ ] **Step 1: Write the failing test**

`tests/test_search.py`:

```python
import hashlib

import pytest

from kbase.db import Database
from kbase.indexer import index_document
from kbase.search import cosine, search_collection
from kbase.store import CollectionStore, DocumentStore


@pytest.fixture
async def db(tmp_path):
    d = Database(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await d.create_all()
    yield d
    await d.dispose()


def keyword_embedder(vocabulary: list[str]):
    """A deterministic stand-in: one dimension per vocabulary word, set when the
    word appears. Cosine over it behaves like real embeddings for these tests
    without a network call or a model."""

    async def embed(texts):
        vectors = []
        for t in texts:
            low = t.lower()
            vectors.append([1.0 if w in low else 0.0 for w in vocabulary])
        return vectors, len(texts)

    return embed


VOCAB = ["bảo hành", "đổi trả", "giao hàng", "mèo"]


async def _seed(db, tenant: str, name: str, body: str) -> str:
    cols = CollectionStore(db)
    await cols.create(tenant, name)
    cid = await cols.resolve_id(tenant, name)
    data = body.encode()
    doc, _ = await DocumentStore(db).create(
        cid, title="Sổ tay", filename="s.md", mime="text/markdown",
        sha256=hashlib.sha256(data).hexdigest(), data=data,
    )
    await index_document(db, doc["id"], embed=keyword_embedder(VOCAB))
    return cid


def test_cosine_basics():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


async def test_finds_the_relevant_chunk(db):
    cid = await _seed(
        db, "acme", "faq",
        "## Bảo hành\n\nbảo hành mười hai tháng.\n\n## Giao hàng\n\ngiao hàng 3 ngày.\n",
    )
    hits, tokens = await search_collection(
        db, cid, "bảo hành", embed=keyword_embedder(VOCAB), limit=5, min_score=0.1
    )
    assert hits
    assert "mười hai tháng" in hits[0]["text"]
    assert hits[0]["heading"] == "Bảo hành"
    assert hits[0]["title"] == "Sổ tay"
    assert tokens == 1


async def test_unrelated_query_returns_nothing_thanks_to_the_floor(db):
    # Without a floor top-k always returns something, and the assistant reads
    # the warranty policy out loud in answer to a question about cats.
    cid = await _seed(db, "acme", "faq", "## Bảo hành\n\nbảo hành mười hai tháng.\n")
    hits, _ = await search_collection(
        db, cid, "mèo", embed=keyword_embedder(VOCAB), limit=5, min_score=0.35
    )
    assert hits == []


async def test_limit_is_respected(db):
    body = "\n\n".join(f"## M{i}\n\nbảo hành mục {i}" for i in range(10))
    cid = await _seed(db, "acme", "faq", body)
    hits, _ = await search_collection(
        db, cid, "bảo hành", embed=keyword_embedder(VOCAB), limit=3, min_score=0.1
    )
    assert len(hits) == 3


async def test_results_are_sorted_by_descending_score(db):
    cid = await _seed(
        db, "acme", "faq",
        "## A\n\nbảo hành đổi trả\n\n## B\n\nbảo hành\n",
    )
    hits, _ = await search_collection(
        db, cid, "bảo hành đổi trả", embed=keyword_embedder(VOCAB), limit=5, min_score=0.1
    )
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True)


async def test_search_never_crosses_collections(db):
    acme = await _seed(db, "acme", "faq", "## Bảo hành\n\nbảo hành của acme\n")
    globex = await _seed(db, "globex", "faq", "## Bảo hành\n\nbảo hành của globex\n")
    hits, _ = await search_collection(
        db, acme, "bảo hành", embed=keyword_embedder(VOCAB), limit=5, min_score=0.1
    )
    assert all("globex" not in h["text"] for h in hits)
    assert globex != acme


async def test_empty_collection_returns_no_hits(db):
    cols = CollectionStore(db)
    await cols.create("acme", "empty")
    cid = await cols.resolve_id("acme", "empty")
    hits, _ = await search_collection(
        db, cid, "bảo hành", embed=keyword_embedder(VOCAB), limit=5, min_score=0.1
    )
    assert hits == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_search.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kbase.search'`

- [ ] **Step 3: Write the implementation**

`src/kbase/search.py`:

```python
"""Embed the query, score every chunk in the collection, apply the floor.

On SQLite this is a scan in Python, which is what the corpus this service is
built for actually needs. The interface is the seam a pgvector implementation
slots into later without any caller noticing.
"""

from __future__ import annotations

import math

from sqlalchemy import select

from kbase.db import Database
from kbase.embedding import Embedder
from kbase.models import Chunk, Document


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


async def search_collection(
    db: Database,
    collection_id: str,
    query: str,
    *,
    embed: Embedder,
    limit: int = 5,
    min_score: float = 0.35,
) -> tuple[list[dict], int]:
    if not query.strip():
        return [], 0
    vectors, tokens = await embed([query])
    if not vectors:
        return [], tokens
    qvec = vectors[0]

    async with db.session() as s:
        rows = (
            await s.execute(
                select(Chunk, Document)
                .join(Document, Chunk.document_id == Document.id)
                .where(Document.collection_id == collection_id, Document.status == "indexed")
            )
        ).all()

    scored: list[dict] = []
    for chunk, doc in rows:
        score = cosine(qvec, list(chunk.embedding or []))
        if score < min_score:
            continue
        scored.append(
            {
                "text": chunk.text,
                "score": score,
                "document_id": doc.id,
                "title": doc.title,
                "filename": doc.filename,
                "heading": chunk.heading,
            }
        )
    scored.sort(key=lambda h: h["score"], reverse=True)
    return scored[:limit], tokens
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_search.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/kbase/search.py tests/test_search.py
git -c user.name=lugondev -c user.email=lugondev@gmail.com \
  commit -q -m "feat(search): cosine top-k with a relevance floor that is never optional"
```

---

### Task 7: Auth and the HTTP surface

**Files:**
- Create: `servers/knowledge-api/src/kbase/types.py`
- Create: `servers/knowledge-api/src/kbase/server/__init__.py`
- Create: `servers/knowledge-api/src/kbase/server/auth.py`
- Create: `servers/knowledge-api/src/kbase/server/app.py`
- Create: `servers/knowledge-api/src/kbase/server/routes.py`
- Modify: `servers/knowledge-api/src/kbase/store.py` (add `CollectionStore.owns`)
- Create: `servers/knowledge-api/tests/conftest.py`
- Test: `servers/knowledge-api/tests/test_auth.py`
- Test: `servers/knowledge-api/tests/test_documents.py`

**Interfaces:**
- Consumes: everything from Tasks 1–6.
- Produces:
  - `types.CreateCollection {name: str}`, `types.CreateTextDocument {collection: str, title: str = "", text: str}`, `types.SearchRequest {collection: str, query: str, limit: int = 5, min_score: float = 0.35}`.
  - `server.auth.require_tenant(request) -> str` — a FastAPI dependency raising 401.
  - `store.CollectionStore.owns(tenant: str, collection_id: str) -> bool` — one query, so an ownership check does not walk every collection the tenant has.
  - `server.app.create_app(settings: Settings, *, embedder: Embedder | None = None) -> FastAPI`, storing `app.state.db`, `app.state.settings`, `app.state.embedder`.

- [ ] **Step 1: Write the failing tests**

`tests/conftest.py`:

```python
import pytest
from fastapi.testclient import TestClient

from kbase.server.app import create_app
from kbase.settings import Settings

VOCAB = ["bảo hành", "đổi trả", "giao hàng", "mèo"]


def keyword_embedder(vocabulary=VOCAB):
    async def embed(texts):
        vectors = []
        for t in texts:
            low = t.lower()
            vectors.append([1.0 if w in low else 0.0 for w in vocabulary])
        return vectors, len(texts)

    return embed


@pytest.fixture
def settings(tmp_path):
    return Settings.from_env({
        "KB_API_KEYS": "acme-key:acme, globex-key:globex",
        "KB_DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path}/api.db",
        "KB_EMBED_BASE_URL": "http://embed.invalid/v1",
        "KB_EMBED_MODEL": "fake",
        "KB_MAX_UPLOAD_BYTES": "1000",
    })


@pytest.fixture
def client(settings):
    app = create_app(settings, embedder=keyword_embedder())
    with TestClient(app) as c:
        yield c


@pytest.fixture
def acme():
    return {"Authorization": "Bearer acme-key"}


@pytest.fixture
def globex():
    return {"Authorization": "Bearer globex-key"}
```

`tests/test_auth.py`:

```python
from fastapi.testclient import TestClient

from kbase.server.app import create_app
from kbase.settings import Settings


def test_no_credential_is_401(client):
    assert client.get("/v1/collections").status_code == 401


def test_wrong_key_is_401(client):
    r = client.get("/v1/collections", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_malformed_header_is_401(client):
    r = client.get("/v1/collections", headers={"Authorization": "acme-key"})
    assert r.status_code == 401


def test_valid_key_is_accepted(client, acme):
    assert client.get("/v1/collections", headers=acme).status_code == 200


def test_healthz_needs_no_credential(client):
    assert client.get("/healthz").status_code == 200


def test_no_configured_keys_rejects_everything(tmp_path):
    # An unset KB_API_KEYS must lock the service, not open it.
    s = Settings.from_env({"KB_DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path}/x.db"})
    with TestClient(create_app(s)) as c:
        assert c.get("/v1/collections", headers={"Authorization": "Bearer any"}).status_code == 401
```

`tests/test_documents.py`:

```python
def _make_collection(client, headers, name="faq"):
    r = client.post("/v1/collections", json={"name": name}, headers=headers)
    assert r.status_code == 201
    return r.json()


def test_collection_lifecycle(client, acme):
    _make_collection(client, acme)
    assert client.get("/v1/collections", headers=acme).json() == [
        {"name": "faq", "document_count": 0}
    ]
    assert client.delete("/v1/collections/faq", headers=acme).status_code == 204
    assert client.get("/v1/collections", headers=acme).json() == []


def test_two_tenants_do_not_see_each_others_collections(client, acme, globex):
    _make_collection(client, acme)
    _make_collection(client, globex)
    client.post(
        "/v1/documents",
        json={"collection": "faq", "title": "acme doc", "text": "## Bảo hành\n\nbảo hành acme"},
        headers=acme,
    )
    assert client.get("/v1/documents?collection=faq", headers=globex).json() == []


def test_tenant_in_the_body_cannot_widen_access(client, acme, globex):
    # The body is a claim; the credential is the decision.
    _make_collection(client, globex)
    r = client.post(
        "/v1/documents",
        json={"collection": "faq", "text": "x", "tenant": "globex"},
        headers=acme,
    )
    assert r.status_code == 404


def test_text_document_indexes_synchronously_enough_to_search(client, acme):
    _make_collection(client, acme)
    r = client.post(
        "/v1/documents",
        json={"collection": "faq", "title": "Sổ tay", "text": "## Bảo hành\n\nbảo hành 12 tháng"},
        headers=acme,
    )
    assert r.status_code == 202
    doc_id = r.json()["id"]
    assert r.json()["status"] == "pending"
    # FastAPI runs background tasks before TestClient returns, so by now it is done.
    doc = client.get(f"/v1/documents/{doc_id}", headers=acme).json()
    assert doc["status"] == "indexed"
    assert doc["chunk_count"] == 1


def test_search_returns_hits_with_usage(client, acme):
    _make_collection(client, acme)
    client.post(
        "/v1/documents",
        json={"collection": "faq", "title": "Sổ tay", "text": "## Bảo hành\n\nbảo hành 12 tháng"},
        headers=acme,
    )
    r = client.post(
        "/v1/search",
        json={"collection": "faq", "query": "bảo hành", "min_score": 0.1},
        headers=acme,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["chunks"][0]["heading"] == "Bảo hành"
    assert body["usage"]["prompt_tokens"] == 1


def test_search_on_a_missing_collection_is_404(client, acme):
    r = client.post("/v1/search", json={"collection": "nope", "query": "x"}, headers=acme)
    assert r.status_code == 404


def test_upload_file(client, acme):
    _make_collection(client, acme)
    r = client.post(
        "/v1/documents",
        data={"collection": "faq", "title": "Sổ tay"},
        files={"file": ("s.md", "## Bảo hành\n\nbảo hành 12 tháng".encode(), "text/markdown")},
        headers=acme,
    )
    assert r.status_code == 202
    doc = client.get(f"/v1/documents/{r.json()['id']}", headers=acme).json()
    assert doc["status"] == "indexed"


def test_identical_upload_returns_the_existing_document(client, acme):
    _make_collection(client, acme)
    payload = {"collection": "faq", "title": "Sổ tay", "text": "## A\n\nbảo hành"}
    first = client.post("/v1/documents", json=payload, headers=acme)
    second = client.post("/v1/documents", json=payload, headers=acme)
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert len(client.get("/v1/documents?collection=faq", headers=acme).json()) == 1


def test_oversized_upload_is_rejected(client, acme):
    _make_collection(client, acme)
    r = client.post(
        "/v1/documents",
        json={"collection": "faq", "text": "x" * 2000},   # KB_MAX_UPLOAD_BYTES=1000
        headers=acme,
    )
    assert r.status_code == 413


def test_unsupported_file_type_is_recorded_as_failed(client, acme):
    _make_collection(client, acme)
    r = client.post(
        "/v1/documents",
        data={"collection": "faq"},
        files={"file": ("manual.pdf", b"%PDF-1.4", "application/pdf")},
        headers=acme,
    )
    doc = client.get(f"/v1/documents/{r.json()['id']}", headers=acme).json()
    assert doc["status"] == "failed"
    assert "pdf" in doc["error"].lower()


def test_document_of_another_tenant_is_404_not_403(client, acme, globex):
    _make_collection(client, acme)
    r = client.post(
        "/v1/documents", json={"collection": "faq", "text": "## A\n\nbảo hành"}, headers=acme
    )
    doc_id = r.json()["id"]
    assert client.get(f"/v1/documents/{doc_id}", headers=globex).status_code == 404
    assert client.delete(f"/v1/documents/{doc_id}", headers=globex).status_code == 404


def test_delete_document(client, acme):
    _make_collection(client, acme)
    r = client.post(
        "/v1/documents", json={"collection": "faq", "text": "## A\n\nbảo hành"}, headers=acme
    )
    assert client.delete(f"/v1/documents/{r.json()['id']}", headers=acme).status_code == 204
    assert client.get("/v1/documents?collection=faq", headers=acme).json() == []


def test_document_into_a_missing_collection_is_404(client, acme):
    r = client.post("/v1/documents", json={"collection": "ghost", "text": "x"}, headers=acme)
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_auth.py tests/test_documents.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kbase.server.app'`

- [ ] **Step 3: Write the wire types**

`src/kbase/types.py`:

```python
"""What crosses the wire. Tenant is deliberately absent: it comes from the
credential, and a field for it would only invite a caller to claim one."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class CreateCollection(BaseModel):
    name: str = Field(min_length=1, max_length=128)

    @field_validator("name")
    @classmethod
    def _no_slashes(cls, v: str) -> str:
        v = v.strip()
        if not v or "/" in v:
            raise ValueError("name must be non-empty and must not contain '/'")
        return v


class CreateTextDocument(BaseModel):
    collection: str
    title: str = ""
    text: str


class SearchRequest(BaseModel):
    collection: str
    query: str
    limit: int = Field(default=5, ge=1, le=50)
    min_score: float = Field(default=0.35, ge=0.0, le=1.0)


class SearchHit(BaseModel):
    text: str
    score: float
    document_id: str
    title: str
    filename: str
    heading: str


class Usage(BaseModel):
    prompt_tokens: int = 0


class SearchResponse(BaseModel):
    chunks: list[SearchHit]
    usage: Usage
```

- [ ] **Step 4: Write auth**

`src/kbase/server/__init__.py`: empty file.

`src/kbase/server/auth.py`:

```python
"""Bearer key -> tenant. The only place a tenant is ever decided."""

from __future__ import annotations

from fastapi import HTTPException, Request

UNAUTHORIZED = HTTPException(
    status_code=401,
    detail="missing or invalid credential",
    headers={"WWW-Authenticate": "Bearer"},
)


def require_tenant(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    scheme, _, key = header.partition(" ")
    if scheme.lower() != "bearer" or not key.strip():
        raise UNAUTHORIZED
    tenant = request.app.state.settings.api_keys.get(key.strip())
    if not tenant:
        raise UNAUTHORIZED
    return tenant
```

- [ ] **Step 5: Write the app and routes**

First add the ownership check to `CollectionStore` in `src/kbase/store.py`:

```python
    async def owns(self, tenant: str, collection_id: str) -> bool:
        """Whether this tenant owns that collection id -- one query.

        The alternative, listing the tenant's collections and resolving each one,
        costs a query per collection on a path that runs on every document read.
        """
        async with self._db.session() as s:
            found = (
                await s.execute(
                    select(Collection.id).where(
                        Collection.id == collection_id, Collection.tenant == tenant
                    )
                )
            ).scalar_one_or_none()
            return found is not None
```

`src/kbase/server/app.py`:

```python
"""The application object, and the state every route reads from."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from kbase.db import Database
from kbase.embedding import Embedder, make_embedder
from kbase.server.routes import router
from kbase.settings import Settings


def create_app(settings: Settings, *, embedder: Embedder | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(settings.database_url)
        await db.create_all()
        app.state.db = db
        try:
            yield
        finally:
            await db.dispose()

    app = FastAPI(
        title="kbase",
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    app.state.settings = settings
    app.state.embedder = embedder or make_embedder(
        base_url=settings.embed_base_url,
        api_key=settings.embed_api_key,
        model=settings.embed_model,
    )
    app.include_router(router)
    return app
```

`src/kbase/server/routes.py`:

```python
"""Every endpoint. A collection is always resolved through the caller's tenant
before anything else happens, so no handler can be reached with someone else's
row in hand."""

from __future__ import annotations

import hashlib

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    Response,
    UploadFile,
)

from kbase.indexer import index_document
from kbase.search import search_collection
from kbase.server.auth import require_tenant
from kbase.store import CollectionStore, DocumentStore
from kbase.types import CreateCollection, CreateTextDocument, SearchRequest

router = APIRouter()


def _stores(request: Request) -> tuple[CollectionStore, DocumentStore]:
    db = request.app.state.db
    return CollectionStore(db), DocumentStore(db)


async def _collection_id_or_404(request: Request, tenant: str, name: str) -> str:
    cols, _ = _stores(request)
    cid = await cols.resolve_id(tenant, name)
    if cid is None:
        raise HTTPException(status_code=404, detail="collection not found")
    return cid


@router.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@router.post("/v1/collections", status_code=201)
async def create_collection(
    body: CreateCollection, request: Request, tenant: str = Depends(require_tenant)
) -> dict:
    cols, _ = _stores(request)
    return await cols.create(tenant, body.name)


@router.get("/v1/collections")
async def list_collections(
    request: Request, tenant: str = Depends(require_tenant)
) -> list[dict]:
    cols, _ = _stores(request)
    return await cols.list(tenant)


@router.delete("/v1/collections/{name}", status_code=204)
async def delete_collection(
    name: str, request: Request, tenant: str = Depends(require_tenant)
) -> Response:
    cols, _ = _stores(request)
    if not await cols.delete(tenant, name):
        raise HTTPException(status_code=404, detail="collection not found")
    return Response(status_code=204)


@router.post("/v1/documents")
async def create_document(
    request: Request,
    background: BackgroundTasks,
    response: Response,
    tenant: str = Depends(require_tenant),
) -> dict:
    """Accepts multipart (a file) or JSON (pasted text).

    The content type is dispatched by hand rather than by declaring both `File`
    and `Form` parameters. With optional form parameters declared, FastAPI parses
    every request body as a form first -- including the JSON ones -- and the two
    shapes end up fighting over the same body. Reading the header and choosing is
    both shorter and unambiguous.
    """
    settings = request.app.state.settings
    content_type = (request.headers.get("content-type") or "").lower()

    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise HTTPException(status_code=422, detail="file is required")
        coll_name = str(form.get("collection") or "")
        if not coll_name:
            raise HTTPException(status_code=422, detail="collection is required")
        data = await upload.read()
        filename = upload.filename or ""
        mime = upload.content_type or ""
        doc_title = str(form.get("title") or "") or filename
    else:
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(status_code=422, detail="body must be JSON or multipart") from None
        body = CreateTextDocument.model_validate(payload)
        data = body.text.encode("utf-8")
        filename = "text.md"
        mime = "text/markdown"
        doc_title = body.title
        coll_name = body.collection

    if len(data) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"document exceeds KB_MAX_UPLOAD_BYTES ({settings.max_upload_bytes})",
        )

    cid = await _collection_id_or_404(request, tenant, coll_name)
    _, docs = _stores(request)
    doc, created = await docs.create(
        cid,
        title=doc_title,
        filename=filename,
        mime=mime,
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    )
    if not created:
        response.status_code = 200
        return doc
    background.add_task(
        index_document, request.app.state.db, doc["id"], embed=request.app.state.embedder
    )
    response.status_code = 202
    return doc


@router.get("/v1/documents")
async def list_documents(
    collection: str,
    request: Request,
    status: str | None = None,
    tenant: str = Depends(require_tenant),
) -> list[dict]:
    cols, docs = _stores(request)
    cid = await cols.resolve_id(tenant, collection)
    if cid is None:
        return []
    return await docs.list(cid, status=status)


async def _owned_document_or_404(request: Request, tenant: str, document_id: str) -> dict:
    cols, docs = _stores(request)
    doc = await docs.get(document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    cid = await docs.owner_collection_id(document_id)
    if cid is None or not await cols.owns(tenant, cid):
        # 404 rather than 403: confirming a document exists but belongs to
        # someone else is itself a leak.
        raise HTTPException(status_code=404, detail="document not found")
    return doc


@router.get("/v1/documents/{document_id}")
async def get_document(
    document_id: str, request: Request, tenant: str = Depends(require_tenant)
) -> dict:
    return await _owned_document_or_404(request, tenant, document_id)


@router.delete("/v1/documents/{document_id}", status_code=204)
async def delete_document(
    document_id: str, request: Request, tenant: str = Depends(require_tenant)
) -> Response:
    await _owned_document_or_404(request, tenant, document_id)
    _, docs = _stores(request)
    await docs.delete(document_id)
    return Response(status_code=204)


@router.post("/v1/search")
async def search(
    body: SearchRequest, request: Request, tenant: str = Depends(require_tenant)
) -> dict:
    cid = await _collection_id_or_404(request, tenant, body.collection)
    hits, tokens = await search_collection(
        request.app.state.db,
        cid,
        body.query,
        embed=request.app.state.embedder,
        limit=body.limit,
        min_score=body.min_score,
    )
    return {"chunks": hits, "usage": {"prompt_tokens": tokens}}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_auth.py tests/test_documents.py -v`
Expected: PASS (6 + 13 tests)

- [ ] **Step 7: Run the whole suite**

Run: `.venv/bin/pytest -q`
Expected: PASS, everything from Tasks 1–6 still green.

- [ ] **Step 8: Commit**

```bash
git add src/kbase/types.py src/kbase/server src/kbase/store.py tests/conftest.py tests/test_auth.py tests/test_documents.py
git -c user.name=lugondev -c user.email=lugondev@gmail.com \
  commit -q -m "feat(api): collections, documents, and search behind a tenant-scoped bearer key"
```

---

### Task 8: CLI, Docker, and documentation

**Files:**
- Create: `servers/knowledge-api/src/kbase/cli.py`
- Create: `servers/knowledge-api/Dockerfile`
- Create: `servers/knowledge-api/docker-compose.yml`
- Create: `servers/knowledge-api/.env.example`
- Create: `servers/knowledge-api/README.md`
- Test: `servers/knowledge-api/tests/test_cli.py`

**Interfaces:**
- Consumes: `Settings` (Task 1), `create_app` (Task 7).
- Produces: `cli.main(argv: list[str] | None = None) -> int` — `kb doctor` returns 0 when `Settings.check()` is empty and 1 otherwise; `kb serve` refuses (returns 1) on a failing check before binding a port.

- [ ] **Step 1: Write the failing test**

`tests/test_cli.py`:

```python
from kbase.cli import main


def test_doctor_reports_every_problem_and_fails(capsys, monkeypatch):
    monkeypatch.delenv("KB_API_KEYS", raising=False)
    monkeypatch.delenv("KB_EMBED_BASE_URL", raising=False)
    monkeypatch.delenv("KB_EMBED_MODEL", raising=False)
    code = main(["doctor"])
    out = capsys.readouterr().out
    assert code == 1
    assert "KB_API_KEYS" in out
    assert "KB_EMBED_MODEL" in out


def test_doctor_passes_on_a_complete_environment(capsys, monkeypatch):
    monkeypatch.setenv("KB_API_KEYS", "k:acme")
    monkeypatch.setenv("KB_EMBED_BASE_URL", "http://x/v1")
    monkeypatch.setenv("KB_EMBED_MODEL", "m")
    assert main(["doctor"]) == 0
    assert "ok" in capsys.readouterr().out.lower()


def test_serve_refuses_a_configuration_doctor_would_fail(capsys, monkeypatch):
    # It must fail before binding a port: a service that starts and then 401s
    # every request looks healthy to a load balancer.
    monkeypatch.delenv("KB_API_KEYS", raising=False)
    monkeypatch.delenv("KB_EMBED_MODEL", raising=False)
    assert main(["serve"]) == 1
    assert "refusing to start" in capsys.readouterr().out.lower()


def test_unknown_command_is_an_error(capsys):
    assert main(["nonsense"]) != 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kbase.cli'`

- [ ] **Step 3: Write the CLI**

`src/kbase/cli.py`:

```python
"""`kb doctor` and `kb serve`.

`serve` runs `doctor` first and refuses on a failure, because a service that
boots with no keys configured answers 401 to everything while still reporting
healthy to whatever is watching the port.
"""

from __future__ import annotations

import argparse
import os
import sys

from kbase.settings import Settings


def _doctor(settings: Settings) -> int:
    problems = settings.check()
    if not problems:
        print("ok: configuration is complete")
        return 0
    for p in problems:
        print(f"problem: {p}")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kb")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="check the configuration and exit")
    serve = sub.add_parser("serve", help="run the HTTP service")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8090)

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 2)

    settings = Settings.from_env(os.environ)
    if args.command == "doctor":
        return _doctor(settings)

    if settings.check():
        print("refusing to start:")
        _doctor(settings)
        return 1

    import uvicorn

    from kbase.server.app import create_app

    uvicorn.run(create_app(settings), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_cli.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Write the deployment files**

`Dockerfile`:

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV KB_DATABASE_URL=sqlite+aiosqlite:////data/kbase.db
VOLUME ["/data"]
EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/healthz').status==200 else 1)"

CMD ["kb", "serve", "--host", "0.0.0.0", "--port", "8090"]
```

`docker-compose.yml`:

```yaml
services:
  kbase:
    build: .
    ports:
      - "8090:8090"
    env_file: .env
    volumes:
      - kbase-data:/data

volumes:
  kbase-data:
```

`.env.example`:

```bash
# key:tenant pairs. Unset means every request is rejected with 401.
KB_API_KEYS=change-me-to-something-long:acme

# SQLite by default; a postgresql+asyncpg:// URL switches the store.
KB_DATABASE_URL=sqlite+aiosqlite:////data/kbase.db

# Any OpenAI-compatible /embeddings endpoint.
KB_EMBED_BASE_URL=https://api.openai.com/v1
KB_EMBED_API_KEY=sk-...
KB_EMBED_MODEL=text-embedding-3-small

# Rejected above this, before the bytes are read.
KB_MAX_UPLOAD_BYTES=20000000

# false closes /docs and /redoc, which need no credential.
KB_DOCS=true
```

- [ ] **Step 6: Write the README**

`README.md`:

````markdown
# kbase — Knowledge Base Service

Documents in, retrievable chunks out. One collection per body of knowledge, one
bearer key per tenant, and a semantic search that refuses to answer with
something irrelevant.

## Run it

```bash
pip install -e ".[dev]"

export KB_API_KEYS=pick-a-long-random-string:acme
export KB_EMBED_BASE_URL=https://api.openai.com/v1
export KB_EMBED_API_KEY=sk-...
export KB_EMBED_MODEL=text-embedding-3-small

kb doctor     # says what is missing, and nothing else
kb serve      # refuses to start on a configuration doctor already failed
```

```bash
AUTH="Authorization: Bearer pick-a-long-random-string"

curl -X POST localhost:8090/v1/collections -H "$AUTH" \
  -H 'Content-Type: application/json' -d '{"name":"faq"}'

curl -X POST localhost:8090/v1/documents -H "$AUTH" \
  -H 'Content-Type: application/json' \
  -d '{"collection":"faq","title":"Sổ tay","text":"## Bảo hành\n\nMười hai tháng."}'

curl -X POST localhost:8090/v1/search -H "$AUTH" \
  -H 'Content-Type: application/json' \
  -d '{"collection":"faq","query":"bảo hành bao lâu"}'
```

Or the whole thing in one file:

```bash
cp .env.example .env      # put your keys in it
docker compose up -d
```

## How it works

An upload is stored and returns `202 pending` immediately; extraction, chunking,
and embedding happen in the background. Embedding a large document takes minutes,
and a synchronous upload would time out before it finished. Poll
`GET /v1/documents/{id}` for `status`.

Chunks are cut on markdown headings first and length second (800 characters, 100
of overlap), and each one keeps the heading path it came from, so an answer can
cite *Bảo hành > Đổi trả* rather than a floating paragraph.

A document either indexes completely or is marked `failed` with a reason. There
is no partial index: a document holding only its first third would answer
questions from that third and never say the rest is missing.

## Configuration

| | |
| --- | --- |
| `KB_API_KEYS` | `key:tenant,key:tenant`. Unset means every request is a 401 |
| `KB_DATABASE_URL` | SQLite by default; a Postgres URL switches the store |
| `KB_EMBED_BASE_URL` | OpenAI-compatible `/embeddings` endpoint |
| `KB_EMBED_API_KEY` | credential for the above |
| `KB_EMBED_MODEL` | embedding model id |
| `KB_MAX_UPLOAD_BYTES` | rejected above this (default 20 MB) |
| `KB_DOCS` | `false` closes `/docs` and `/redoc`, which need no credential |

## API

```
POST   /v1/collections            {name}
GET    /v1/collections
DELETE /v1/collections/{name}

POST   /v1/documents              multipart (file, collection, title?)
                                  or JSON {collection, title, text}
GET    /v1/documents?collection=&status=
GET    /v1/documents/{id}
DELETE /v1/documents/{id}

POST   /v1/search                 {collection, query, limit, min_score}
GET    /healthz
```

## Not here yet

PDF and DOCX ingestion, URL crawling, hybrid keyword search, and re-ranking.
Gateway integration is specified in the parent repository's spec and deliberately
not built yet.

## Tests

```bash
.venv/bin/pytest -q
```
````

- [ ] **Step 7: Run the whole suite**

Run: `.venv/bin/pytest -q`
Expected: PASS — all tests from Tasks 1–8.

- [ ] **Step 8: Verify the service actually starts**

```bash
KB_API_KEYS=t:acme KB_EMBED_BASE_URL=http://x/v1 KB_EMBED_MODEL=m \
  .venv/bin/kb doctor
```
Expected: prints `ok: configuration is complete`, exit code 0.

- [ ] **Step 9: Commit**

```bash
git add src/kbase/cli.py tests/test_cli.py Dockerfile docker-compose.yml .env.example README.md
git -c user.name=lugondev -c user.email=lugondev@gmail.com \
  commit -q -m "feat(cli): doctor and serve, plus Docker and docs"
```

---

### Task 9: Lint and a final green run

**Files:**
- Modify: `servers/knowledge-api/pyproject.toml` (ruff config)

- [ ] **Step 1: Add ruff configuration**

Append to `pyproject.toml`:

```toml
[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]
```

- [ ] **Step 2: Run the linter and fix what it finds**

Run: `.venv/bin/ruff check . && .venv/bin/ruff format --check .`
Expected: clean. Fix anything reported; do not silence it with `noqa` unless the
existing `noqa: BLE001` in `indexer.py` is the case (a deliberate broad catch).

- [ ] **Step 3: Run the whole suite one final time**

Run: `.venv/bin/pytest -q`
Expected: PASS, 0 failures.

- [ ] **Step 4: Confirm the parent repository is untouched**

```bash
cd /Users/lugon/code/speech-text-transformer/.claude/worktrees/knowledge-api
git status --short
```
Expected: only `servers/knowledge-api/` shows as an untracked directory (it is its
own git repository), plus the plan and spec files already committed. Nothing under
`apps/` or `tests/` may appear.

- [ ] **Step 5: Commit**

```bash
cd servers/knowledge-api
git add pyproject.toml
git -c user.name=lugondev -c user.email=lugondev@gmail.com \
  commit -q -m "chore: ruff configuration"
```

---

## After the plan

The service repository lives inside the worktree at
`.claude/worktrees/knowledge-api/servers/knowledge-api`. **Removing the worktree
deletes it.** Before any worktree cleanup, either move the directory into the main
checkout or push it to a remote. Registering it as a submodule of the parent
repository requires a remote and is deliberately out of this plan's scope.
