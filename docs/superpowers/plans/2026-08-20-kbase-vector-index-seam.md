# kbase Vector Index Seam Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put a real `ChunkIndex` seam under kbase's vector storage and add a pgvector implementation behind it, without changing what the service does on SQLite.

**Architecture:** A `ChunkIndex` protocol owns every read and write that touches an embedding. `SqlScanIndex` is today's numpy partition scan, unchanged. `PgVectorIndex` stores `vector(n)`, builds an HNSW index, and orders by distance in the database. The stores keep metadata and orchestration, pass their own session into the index for writes, and never write vector SQL themselves.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2 async, aiosqlite, asyncpg, pgvector, numpy, pytest (asyncio_mode=auto), ruff.

**Spec:** `docs/superpowers/specs/2026-08-20-kbase-vector-index-seam-design.md`

## Global Constraints

- All work happens in the `servers/knowledge-api` submodule. Nothing outside it changes.
- Create and stay on branch `feat/pgvector-index-seam` in that submodule. Do not commit to its `main`.
- Run tests from `servers/knowledge-api` with `.venv/bin/pytest`.
- **Run only the specific test you are working on** while implementing (`pytest tests/test_x.py::test_y -v`). Run the full suite once, at the end of a task, before committing.
- **Every new test must be observed failing before its implementation is written.** A test that has never been red is not evidence. This repo has a documented history of tests that could not fail.
- Existing test *assertions* must not be edited. Import lines and call signatures may be updated where a task changes an interface; if you feel the need to change what an assertion claims, stop — the refactor broke something.
- ruff: line-length 100, target py311, rules `E,F,I,UP,B`. Run `.venv/bin/ruff check src tests` before each commit.
- Python 3.11+ syntax; `from __future__ import annotations` at the top of every module, matching the existing files.
- Commit as `lugondev <lugondev@gmail.com>`. Do not push.

---

### Task 1: `collection_id` on chunks, and a schema step that can add a column

The pgvector query must filter on a single table. That requires `collection_id`
on `chunks`. `Base.metadata.create_all` creates missing *tables* and never
missing *columns*, so an existing deployment needs the column added and
backfilled or every insert fails against a column that is not there.

**Files:**
- Modify: `src/kbase/models.py` (`Chunk`)
- Modify: `src/kbase/db.py` (`create_all`)
- Modify: `src/kbase/store.py:323` (`replace_chunks`)
- Modify: `src/kbase/indexer.py:68`
- Modify: `tests/test_audit_fixes.py:210,413,441` (call signature only)
- Test: `tests/test_schema_upgrade.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `Chunk.collection_id: str`; `DocumentStore.replace_chunks(document_id: str, collection_id: str, rows: list[dict]) -> bool`; `Database.create_all()` now also adds missing columns.

- [ ] **Step 1: Write the failing test**

Create `tests/test_schema_upgrade.py`:

```python
"""A database written before a column existed still has to work."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from kbase.db import Database
from kbase.store import CollectionStore, DocumentStore


@pytest.fixture
async def db(tmp_path):
    d = Database(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await d.create_all()
    yield d
    await d.dispose()


async def test_missing_collection_id_column_is_added_and_backfilled(db):
    cols = CollectionStore(db)
    await cols.create("acme", "faq")
    cid = await cols.resolve_id("acme", "faq")
    docs = DocumentStore(db)
    doc, _ = await docs.create(
        cid, title="t", filename="f.md", mime="text/markdown", sha256="a" * 64, data=b"x"
    )
    await docs.replace_chunks(
        doc["id"], cid, [{"ordinal": 0, "text": "hi", "heading": "", "embedding": [1.0]}]
    )

    # Rewind to the schema as it was before this column existed. SQLite refuses
    # to drop an indexed column, so the index goes first.
    async with db.session() as s:
        await s.execute(text("DROP INDEX IF EXISTS ix_chunks_collection_id"))
        await s.execute(text("ALTER TABLE chunks DROP COLUMN collection_id"))
        await s.commit()

    await db.create_all()

    async with db.session() as s:
        got = (await s.execute(text("SELECT collection_id FROM chunks"))).scalars().all()
    assert got == [cid]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_schema_upgrade.py -v`
Expected: FAIL — `replace_chunks() takes 3 positional arguments but 4 were given`.

- [ ] **Step 3: Add the column to the model**

In `src/kbase/models.py`, inside `class Chunk`, after `document_id`:

```python
    # Denormalized from the owning document so a vector search filters on one
    # table. Under an ANN index a filter that lives on a joined table forces
    # over-fetch and post-filter, and recall drops exactly when the collection
    # is large enough to have needed the index.
    collection_id: Mapped[str] = mapped_column(String(36), index=True, default="")
```

- [ ] **Step 4: Teach `create_all` to add a missing column**

Replace `create_all` in `src/kbase/db.py` and add the helper:

```python
    async def create_all(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(self._ensure_chunk_collection_id)

    @staticmethod
    def _ensure_chunk_collection_id(conn) -> None:
        """`create_all` creates missing tables, never missing columns.

        A deployment that indexed anything before `chunks.collection_id`
        existed keeps a table the ORM no longer matches, and every insert
        fails against a column that is not there.
        """
        inspector = sa_inspect(conn)
        if "chunks" not in inspector.get_table_names():
            return
        if "collection_id" in {c["name"] for c in inspector.get_columns("chunks")}:
            return
        conn.execute(text("ALTER TABLE chunks ADD COLUMN collection_id VARCHAR(36) DEFAULT ''"))
        conn.execute(
            text(
                "UPDATE chunks SET collection_id = (SELECT collection_id FROM documents "
                "WHERE documents.id = chunks.document_id) "
                "WHERE collection_id IS NULL OR collection_id = ''"
            )
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_chunks_collection_id ON chunks (collection_id)")
        )
```

Add to the imports at the top of `src/kbase/db.py`:

```python
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
```

- [ ] **Step 5: Thread `collection_id` through the write path**

In `src/kbase/store.py`, change the signature and the row construction:

```python
    async def replace_chunks(
        self, document_id: str, collection_id: str, rows: list[dict]
    ) -> bool:
```

and inside the loop that adds each `Chunk(...)`, add the argument:

```python
                        collection_id=collection_id,
```

In `src/kbase/indexer.py`, immediately before the `written = await docs.replace_chunks(` call:

```python
        collection_id = await docs.owner_collection_id(document_id)
        if collection_id is None:
            logger.info("document %s was deleted while it was being indexed", document_id)
            return
```

and pass it as the second argument:

```python
        written = await docs.replace_chunks(
            document_id,
            collection_id,
            [
```

- [ ] **Step 6: Update the three existing call sites**

In `tests/test_audit_fixes.py`, each of the three `await docs.replace_chunks(` calls (lines 210, 413, 441) gains the collection id as its second argument. Each of those tests already has the collection id in scope — use the same variable the surrounding test uses to create the document. Change only the call, not the assertions.

- [ ] **Step 7: Run the new test, then the whole suite**

Run: `.venv/bin/pytest tests/test_schema_upgrade.py -v`
Expected: PASS

Run: `.venv/bin/pytest -q && .venv/bin/ruff check src tests`
Expected: all pass, no lint errors.

- [ ] **Step 8: Commit**

```bash
git add src/kbase/models.py src/kbase/db.py src/kbase/store.py src/kbase/indexer.py \
        tests/test_schema_upgrade.py tests/test_audit_fixes.py
git commit -m "feat(store): chunks carry their collection id, and create_all can add it"
```

---

### Task 2: Chunks exist only for indexed documents

Today `search` hides a reindexing document's stale chunks with a
`status == 'indexed'` predicate on a joined table. Delete the chunks instead and
the predicate is unnecessary — which is what lets the pgvector query filter on
`chunks` alone.

This is behaviour-preserving: a document being reindexed is invisible to search
today because its status is `pending`; afterwards it is invisible because its
chunks are gone.

**Files:**
- Modify: `src/kbase/store.py:229` (`mark_pending`)
- Modify: `src/kbase/search.py:100` (`search_collection` — the `where` clause)
- Test: `tests/test_index_invariant.py` (create)

**Interfaces:**
- Consumes: `DocumentStore.replace_chunks(document_id, collection_id, rows)` from Task 1.
- Produces: the invariant *chunks exist only for documents whose status is `indexed`*, which Tasks 3 and 6 rely on.

- [ ] **Step 1: Write the failing test**

Create `tests/test_index_invariant.py`:

```python
"""Chunks exist only for documents whose status is `indexed`."""

from __future__ import annotations

import hashlib

import pytest

from kbase.db import Database
from kbase.indexer import index_document
from kbase.store import CollectionStore, DocumentStore


@pytest.fixture
async def db(tmp_path):
    d = Database(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await d.create_all()
    yield d
    await d.dispose()


def fake_embedder():
    async def embed(texts):
        return [[1.0, 0.0, 0.0] for _ in texts], len(texts)

    return embed


async def _indexed_document(db):
    cols = CollectionStore(db)
    await cols.create("acme", "faq")
    cid = await cols.resolve_id("acme", "faq")
    docs = DocumentStore(db)
    body = b"# Heading\n\nsome text"
    doc, _ = await docs.create(
        cid,
        title="t",
        filename="f.md",
        mime="text/markdown",
        sha256=hashlib.sha256(body).hexdigest(),
        data=body,
    )
    await index_document(db, doc["id"], embed=fake_embedder())
    return docs, doc["id"]


async def test_marking_pending_removes_the_chunks(db):
    docs, doc_id = await _indexed_document(db)
    assert await docs.chunks(doc_id) != []

    await docs.mark_pending(doc_id)

    # Not merely hidden from search -- gone. A chunk that outlives its
    # document's `indexed` status is reachable by a query that filters on
    # chunks alone, which is what the pgvector backend does.
    assert await docs.chunks(doc_id) == []


async def test_marking_pending_twice_is_still_empty(db):
    docs, doc_id = await _indexed_document(db)
    await docs.mark_pending(doc_id)
    await docs.mark_pending(doc_id)
    assert await docs.chunks(doc_id) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_index_invariant.py -v`
Expected: `test_marking_pending_removes_the_chunks` FAILS — the chunk list is
non-empty, because `mark_pending` leaves chunks behind today.

- [ ] **Step 3: Delete the chunks in `mark_pending`**

In `src/kbase/store.py`, in `mark_pending`, replace the body after the
`if row.status == "pending": return _doc_dict(row)` guard:

```python
            # The chunks go with the status. Left behind, they would be a
            # complete, searchable copy of a document that is about to be
            # re-embedded -- invisible today only because search filters on a
            # joined `status`, which the pgvector backend cannot afford to do.
            await s.execute(sa_delete(Chunk).where(Chunk.document_id == document_id))
            _reset(row)
            await s.commit()
            return _doc_dict(row)
```

The early return for an already-pending document must stay above this: two
indexers racing over one document is still the thing it prevents.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_index_invariant.py -v`
Expected: PASS (both tests)

- [ ] **Step 5: Drop the status predicate from search**

In `src/kbase/search.py`, in `search_collection`, replace the `stmt`:

```python
    stmt = (
        select(Chunk, Document)
        .join(Document, Chunk.document_id == Document.id)
        # No `status` predicate: a chunk only exists for an indexed document
        # (see store.mark_pending / indexer's failure path). The join is here
        # for the title and filename in the payload, not to filter.
        .where(Chunk.collection_id == collection_id)
        .execution_options(yield_per=PARTITION_SIZE)
    )
```

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check src tests`
Expected: all pass. The existing search and indexer tests are the proof that
dropping the predicate changed nothing observable; if any of them go red, the
invariant does not hold and the cause must be found before continuing.

- [ ] **Step 7: Commit**

```bash
git add src/kbase/store.py src/kbase/search.py tests/test_index_invariant.py
git commit -m "feat(store): chunks live only for indexed documents"
```

---

### Task 3: The `ChunkIndex` seam, with today's scan behind it

A pure refactor. Nothing about behaviour changes; the whole existing suite is
the proof.

Write methods take the caller's `AsyncSession` so a collection delete stays one
transaction, exactly as it is today. `query` opens its own read session. Both
are deliberate consequences of the seam being scoped to SQL backends.

**Files:**
- Create: `src/kbase/index.py`
- Modify: `src/kbase/search.py` (keep `cosine`/`_score_batch`, remove `search_collection`)
- Modify: `src/kbase/store.py` (stores take an index; vector SQL moves out)
- Modify: `src/kbase/server/app.py`, `src/kbase/server/routes.py`, `src/kbase/indexer.py`
- Modify: `tests/test_search.py` (imports and call sites only)
- Test: `tests/test_chunk_index.py` (create)

**Interfaces:**
- Consumes: the Task 2 invariant.
- Produces:
  - `ChunkIndex` protocol with `create_schema()`, `replace(s, document_id, collection_id, rows)`, `drop_document(s, document_id)`, `drop_where_document_in(s, doc_id_select)`, `chunks(document_id)`, `query(collection_id, qvec, *, limit, min_score)`.
  - `SqlScanIndex(db)` implementing it.
  - `CollectionStore(db, index)` and `DocumentStore(db, index)`.
  - `app.state.index`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_chunk_index.py`:

```python
"""The seam's contract, stated once so both backends can be held to it."""

from __future__ import annotations

import pytest

from kbase.db import Database
from kbase.index import SqlScanIndex
from kbase.store import CollectionStore, DocumentStore


@pytest.fixture
async def db(tmp_path):
    d = Database(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await d.create_all()
    yield d
    await d.dispose()


@pytest.fixture
def index(db):
    return SqlScanIndex(db)


async def _collection(db, index, tenant="acme", name="faq"):
    cols = CollectionStore(db, index)
    await cols.create(tenant, name)
    return await cols.resolve_id(tenant, name)


async def _document(db, index, cid, sha):
    docs = DocumentStore(db, index)
    doc, _ = await docs.create(
        cid, title="T", filename="f.md", mime="text/markdown", sha256=sha, data=b"x"
    )
    return docs, doc["id"]


async def test_query_returns_the_nearer_chunk_first(db, index):
    cid = await _collection(db, index)
    docs, doc_id = await _document(db, index, cid, "a" * 64)
    await docs.replace_chunks(
        doc_id,
        cid,
        [
            {"ordinal": 0, "text": "far", "heading": "", "embedding": [0.0, 1.0]},
            {"ordinal": 1, "text": "near", "heading": "", "embedding": [1.0, 0.0]},
        ],
    )
    await docs.mark_indexed(doc_id, 2)

    hits = await index.query(cid, [1.0, 0.0], limit=5, min_score=0.0)

    assert [h["text"] for h in hits] == ["near", "far"]
    assert hits[0]["title"] == "T"


async def test_min_score_is_a_floor(db, index):
    cid = await _collection(db, index)
    docs, doc_id = await _document(db, index, cid, "b" * 64)
    await docs.replace_chunks(
        doc_id,
        cid,
        [
            {"ordinal": 0, "text": "orthogonal", "heading": "", "embedding": [0.0, 1.0]},
            {"ordinal": 1, "text": "same", "heading": "", "embedding": [1.0, 0.0]},
        ],
    )
    await docs.mark_indexed(doc_id, 2)

    hits = await index.query(cid, [1.0, 0.0], limit=5, min_score=0.5)

    assert [h["text"] for h in hits] == ["same"]


async def test_query_never_crosses_collections(db, index):
    mine = await _collection(db, index, "acme", "faq")
    theirs = await _collection(db, index, "globex", "faq")
    docs, doc_id = await _document(db, index, theirs, "c" * 64)
    await docs.replace_chunks(
        doc_id, theirs, [{"ordinal": 0, "text": "secret", "heading": "", "embedding": [1.0, 0.0]}]
    )
    await docs.mark_indexed(doc_id, 1)

    assert await index.query(mine, [1.0, 0.0], limit=5, min_score=0.0) == []


async def test_deleting_the_document_removes_its_chunks(db, index):
    cid = await _collection(db, index)
    docs, doc_id = await _document(db, index, cid, "d" * 64)
    await docs.replace_chunks(
        doc_id, cid, [{"ordinal": 0, "text": "gone", "heading": "", "embedding": [1.0, 0.0]}]
    )
    await docs.mark_indexed(doc_id, 1)

    await docs.delete(doc_id)

    assert await index.query(cid, [1.0, 0.0], limit=5, min_score=0.0) == []
    assert await index.chunks(doc_id) == []


async def test_deleting_the_collection_removes_its_chunks(db, index):
    cid = await _collection(db, index)
    docs, doc_id = await _document(db, index, cid, "e" * 64)
    await docs.replace_chunks(
        doc_id, cid, [{"ordinal": 0, "text": "gone", "heading": "", "embedding": [1.0, 0.0]}]
    )
    await docs.mark_indexed(doc_id, 1)

    await CollectionStore(db, index).delete("acme", "faq")

    assert await index.chunks(doc_id) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_chunk_index.py -v`
Expected: FAIL at import — `cannot import name 'SqlScanIndex' from 'kbase.index'`
(the module does not exist).

- [ ] **Step 3: Write `src/kbase/index.py`**

```python
"""Everything that touches an embedding, behind one interface.

The write methods take the caller's session rather than opening their own: a
collection delete removes chunks, documents and the collection in a single
transaction today, and an index that opened its own connection would break that
into three. `query` owns its session, because a read joins nothing else.

Both are honest consequences of this seam being scoped to SQL backends. An
external store (Qdrant, Vectorize) shares no transaction with the metadata
tables and would need a different shape -- along with the reconcile pass that a
shared transaction makes unnecessary here.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

import numpy as np
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kbase.db import Database
from kbase.models import Chunk, Document
from kbase.search import PARTITION_SIZE, _score_batch, warn_if_large


class ChunkIndex(Protocol):
    async def create_schema(self) -> None: ...

    async def replace(
        self, s: AsyncSession, document_id: str, collection_id: str, rows: list[dict]
    ) -> None: ...

    async def drop_document(self, s: AsyncSession, document_id: str) -> None: ...

    async def drop_where_document_in(self, s: AsyncSession, doc_id_select) -> None: ...

    async def chunks(self, document_id: str) -> list[dict]: ...

    async def query(
        self, collection_id: str, qvec: list[float], *, limit: int, min_score: float
    ) -> list[dict]: ...


class SqlScanIndex:
    """Cosine against every chunk in the collection, a partition at a time."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create_schema(self) -> None:
        """Nothing beyond the tables `Database.create_all` already makes."""
        return None

    async def replace(
        self, s: AsyncSession, document_id: str, collection_id: str, rows: list[dict]
    ) -> None:
        await s.execute(sa_delete(Chunk).where(Chunk.document_id == document_id))
        for r in rows:
            s.add(
                Chunk(
                    document_id=document_id,
                    collection_id=collection_id,
                    ordinal=r["ordinal"],
                    text=r["text"],
                    heading=r["heading"],
                    char_count=len(r["text"]),
                    embedding=r["embedding"],
                )
            )

    async def drop_document(self, s: AsyncSession, document_id: str) -> None:
        await s.execute(sa_delete(Chunk).where(Chunk.document_id == document_id))

    async def drop_where_document_in(self, s: AsyncSession, doc_id_select) -> None:
        await s.execute(sa_delete(Chunk).where(Chunk.document_id.in_(doc_id_select)))

    async def chunks(self, document_id: str) -> list[dict]:
        async with self._db.session() as s:
            rows = (
                (
                    await s.execute(
                        select(Chunk)
                        .where(Chunk.document_id == document_id)
                        .order_by(Chunk.ordinal)
                    )
                )
                .scalars()
                .all()
            )
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

    async def query(
        self, collection_id: str, qvec: list[float], *, limit: int, min_score: float
    ) -> list[dict]:
        vec = np.array(qvec, dtype=np.float64)
        qnorm = float(np.linalg.norm(vec))
        if qnorm == 0.0:
            return []

        stmt = (
            select(Chunk, Document)
            .join(Document, Chunk.document_id == Document.id)
            .where(Chunk.collection_id == collection_id)
            .execution_options(yield_per=PARTITION_SIZE)
        )

        best: list[tuple[float, int, dict]] = []
        scanned = 0
        async with self._db.session() as s:
            result = await s.stream(stmt)
            async for partition in result.partitions(PARTITION_SIZE):
                batch = [
                    (
                        list(chunk.embedding or []),
                        {
                            "text": chunk.text,
                            "document_id": doc.id,
                            "title": doc.title,
                            "filename": doc.filename,
                            "heading": chunk.heading,
                        },
                    )
                    for chunk, doc in partition
                ]
                hits = await asyncio.to_thread(_score_batch, vec, qnorm, batch, min_score, limit)
                best.extend(
                    (score, scanned + i, payload) for i, (score, payload) in enumerate(hits)
                )
                scanned += len(batch)
                if len(best) > limit:
                    best.sort(key=lambda h: (-h[0], h[1]))
                    del best[limit:]

        warn_if_large(collection_id, scanned)
        best.sort(key=lambda h: (-h[0], h[1]))
        return [{**payload, "score": score} for score, _order, payload in best[:limit]]
```

- [ ] **Step 4: Reduce `search.py` to its scoring internals**

In `src/kbase/search.py`: delete `search_collection` entirely, along with the
now-unused imports (`Database`, `Embedder`, `Chunk`, `Document`, `select`,
`asyncio`). Keep the module docstring, `PARTITION_SIZE`, `SCAN_WARN_CHUNKS`,
`_warned`, `cosine`, `_score_batch`, and add the warning helper that
`SqlScanIndex` now calls:

```python
def warn_if_large(collection_id: str, scanned: int) -> None:
    """Say it once per collection, then stop. The fix is an index, not a
    bigger machine."""
    if scanned >= SCAN_WARN_CHUNKS and collection_id not in _warned:
        _warned.add(collection_id)
        logger.warning(
            "collection %s holds %d+ chunks; every search scans all of them. "
            "This store is a linear scan -- move to a vector index.",
            collection_id,
            scanned,
        )
```

- [ ] **Step 5: Move the vector SQL out of `store.py`**

In `src/kbase/store.py`:

Both stores take the index:

```python
class CollectionStore:
    def __init__(self, db: Database, index: ChunkIndex) -> None:
        self._db = db
        self._index = index
```

```python
class DocumentStore:
    def __init__(self, db: Database, index: ChunkIndex) -> None:
        self._db = db
        self._index = index
```

In `CollectionStore.delete`, replace the chunk delete line with the index call,
keeping the subquery and the ordering:

```python
            owned = select(Document.id).where(Document.collection_id == row.id)
            await self._index.drop_where_document_in(s, owned)
            await s.execute(sa_delete(Document).where(Document.collection_id == row.id))
```

In `DocumentStore.delete`, `drop_chunks`, `mark_pending` and
`fail_stale_pending`, replace each `s.execute(sa_delete(Chunk)...)` with the
matching index call — `drop_document(s, document_id)` for the first three,
`drop_where_document_in(s, stranded)` for the last.

`replace_chunks` keeps its existence check and its commit, and delegates only
the write:

```python
    async def replace_chunks(
        self, document_id: str, collection_id: str, rows: list[dict]
    ) -> bool:
        async with self._db.session() as s:
            still_there = (
                await s.execute(select(Document.id).where(Document.id == document_id))
            ).scalar_one_or_none()
            if still_there is None:
                return False
            await self._index.replace(s, document_id, collection_id, rows)
            await s.commit()
            return True
```

Delete `DocumentStore.chunks` — it now lives on the index. Remove the `Chunk`
import if nothing else in the file uses it.

- [ ] **Step 6: Wire it through the app**

In `src/kbase/server/app.py`, inside `lifespan`, after `await db.create_all()`:

```python
        index = SqlScanIndex(db)
        await index.create_schema()
        app.state.index = index
```

and change the sweep to pass the index:

```python
        swept = await DocumentStore(db, index).fail_stale_pending(
```

In `src/kbase/server/routes.py`, delete the now-dead import
`from kbase.search import search_collection` — nothing replaces it, because the
handler reaches the index through `request.app.state` — and change `_stores`:

```python
def _stores(request: Request) -> tuple[CollectionStore, DocumentStore]:
    db = request.app.state.db
    index = request.app.state.index
    return CollectionStore(db, index), DocumentStore(db, index)
```

and the search handler embeds first, then queries:

```python
    cid = await _collection_id_or_404(request, tenant, body.collection)
    if not body.query.strip():
        return {"chunks": [], "usage": {"prompt_tokens": 0}}
    vectors, tokens = await request.app.state.embedder([body.query])
    if not vectors:
        return {"chunks": [], "usage": {"prompt_tokens": tokens}}
    hits = await request.app.state.index.query(
        cid, vectors[0], limit=body.limit, min_score=body.min_score
    )
    return {"chunks": hits, "usage": {"prompt_tokens": tokens}}
```

In `src/kbase/indexer.py`, `index_document` gains the index and passes it to the
store:

```python
async def index_document(
    db: Database,
    index: ChunkIndex,
    document_id: str,
    *,
    embed: Embedder,
    max_chars: int = 800,
    overlap: int = 100,
) -> None:
```

with `docs = DocumentStore(db, index)` as the first line of the body. Update the
two `background.add_task(index_document, request.app.state.db, ...)` calls in
`routes.py` to pass `request.app.state.index` as the second positional argument.

- [ ] **Step 7: Update the existing search tests' call sites**

In `tests/test_search.py`, change the import to

```python
from kbase.index import SqlScanIndex
from kbase.search import cosine
```

and every `await search_collection(db, cid, "query text", embed=emb, ...)` to

```python
vectors, _ = await emb(["query text"])
hits = await SqlScanIndex(db).query(cid, vectors[0], limit=..., min_score=...)
```

taking the `limit` and `min_score` values from the call being replaced, and
unwrapping the old `(hits, tokens)` tuple.

Three interfaces changed, and every test that touches them needs the same
mechanical edit — `index_document(db, index, doc_id, embed=...)`,
`DocumentStore(db, index)`, `CollectionStore(db, index)`, with
`index = SqlScanIndex(db)`. Find them with:

```bash
grep -rln "index_document(\|DocumentStore(\|CollectionStore(" tests/
```

which at minimum covers `tests/test_search.py`, `tests/test_indexer.py`,
`tests/test_audit_fixes.py`, `tests/test_review_fixes.py`,
`tests/test_documents.py`, `tests/test_collections.py`,
`tests/test_index_invariant.py` and `tests/test_schema_upgrade.py`.

**Do not change a single assertion.** Signatures and imports only. If a test
only passes after its expectation is edited, the refactor broke behaviour and
the cause must be found before continuing.

- [ ] **Step 8: Run the new test, then the whole suite**

Run: `.venv/bin/pytest tests/test_chunk_index.py -v`
Expected: PASS (5 tests)

Run: `.venv/bin/pytest -q && .venv/bin/ruff check src tests`
Expected: all pass. This suite passing unchanged is the entire warrant that the
refactor preserved behaviour.

- [ ] **Step 9: Commit**

```bash
git add src/kbase tests
git commit -m "refactor: a ChunkIndex seam, with the scan behind it"
```

---

### Task 4: `KB_EMBED_DIM`, and a doctor that warns without blocking

`vector(n)` needs `n` at DDL time. Setting this variable is what turns pgvector
on; leaving it unset keeps a Postgres deployment on the scan, which is what
makes the upgrade non-breaking.

`Settings.check()` cannot carry warnings — `kb serve` refuses to start on
anything it returns. Warnings need their own channel.

**Files:**
- Modify: `src/kbase/settings.py`
- Modify: `src/kbase/cli.py:17`
- Modify: `.env.example`
- Test: `tests/test_settings.py` (append)

**Interfaces:**
- Consumes: nothing.
- Produces: `Settings.embed_dim: int` (0 when unset) and `Settings.warnings() -> list[str]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_settings.py`:

```python
def test_embed_dim_defaults_to_zero_when_unset():
    s = Settings.from_env({})
    assert s.embed_dim == 0


def test_a_non_numeric_embed_dim_is_a_problem_not_a_guess():
    s = Settings.from_env({"KB_EMBED_DIM": "large"})
    assert any("KB_EMBED_DIM" in p for p in s.check())


def test_postgres_without_a_dimension_warns_that_it_will_scan():
    s = Settings.from_env(
        {
            "KB_API_KEYS": "k:acme",
            "KB_DATABASE_URL": "postgresql+asyncpg://u:p@h/db",
            "KB_EMBED_BASE_URL": "http://e/v1",
            "KB_EMBED_MODEL": "m",
        }
    )
    assert s.check() == []
    assert any("scan" in w for w in s.warnings())


def test_a_dimension_over_the_hnsw_ceiling_warns_but_starts():
    s = Settings.from_env(
        {
            "KB_API_KEYS": "k:acme",
            "KB_DATABASE_URL": "postgresql+asyncpg://u:p@h/db",
            "KB_EMBED_BASE_URL": "http://e/v1",
            "KB_EMBED_MODEL": "m",
            "KB_EMBED_DIM": "3072",
        }
    )
    assert s.check() == []
    assert any("2000" in w for w in s.warnings())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_settings.py -v -k "embed_dim or dimension or scan"`
Expected: FAIL — `Settings` has no attribute `embed_dim`.

- [ ] **Step 3: Implement**

In `src/kbase/settings.py`, add the constant and the field:

```python
#: pgvector will not build an HNSW index on a wider vector. Above this a column
#: still stores and still searches -- by scanning, which is the thing pgvector
#: was brought in to stop doing.
HNSW_MAX_DIMENSIONS = 2000
```

```python
    embed_dim: int = 0
```

In `from_env`, parse it alongside `max_upload`:

```python
        raw_dim = env.get("KB_EMBED_DIM", "").strip()
        try:
            embed_dim = int(raw_dim) if raw_dim else 0
        except ValueError:
            embed_dim = -1  # invalid, and `check` says so rather than guessing
```

and pass `embed_dim=embed_dim` into the constructor.

In `check()`, add:

```python
        if self.embed_dim < 0:
            problems.append("KB_EMBED_DIM must be a positive integer")
```

And the new method:

```python
    def warnings(self) -> list[str]:
        """Things worth saying that are not reasons to refuse to start."""
        notes: list[str] = []
        if self.database_url.startswith("postgresql") and self.embed_dim == 0:
            notes.append(
                "KB_EMBED_DIM is unset on a Postgres database: every search will "
                "scan the collection rather than use a vector index"
            )
        if self.embed_dim > HNSW_MAX_DIMENSIONS:
            notes.append(
                f"KB_EMBED_DIM is {self.embed_dim}: pgvector builds no HNSW index above "
                f"{HNSW_MAX_DIMENSIONS} dimensions, so searches will scan. Reduce the "
                "model's output width instead"
            )
        return notes
```

In `src/kbase/cli.py`, `_doctor` prints them:

```python
def _doctor(settings: Settings) -> int:
    problems = settings.check()
    for w in settings.warnings():
        print(f"warning: {w}")
    if not problems:
        print("ok: configuration is complete")
        return 0
    for p in problems:
        print(f"problem: {p}")
    return 1
```

In `.env.example`, under the embedding block:

```
# Set this to store vectors in a pgvector column instead of scanning. Postgres
# only; must match the model's output width. Above 2000 there is no HNSW index.
KB_EMBED_DIM=1536
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_settings.py -v`
Expected: PASS

- [ ] **Step 5: Run the suite and commit**

```bash
.venv/bin/pytest -q && .venv/bin/ruff check src tests
git add src/kbase/settings.py src/kbase/cli.py .env.example tests/test_settings.py
git commit -m "feat(settings): KB_EMBED_DIM, and warnings that do not block a start"
```

---

### Task 5: A Postgres test harness, and `PgVectorIndex` writes

**Files:**
- Create: `src/kbase/pgindex.py`
- Modify: `src/kbase/db.py` (expose the engine)
- Modify: `src/kbase/errors.py` (add `SchemaError`)
- Modify: `docker-compose.yml`
- Modify: `pyproject.toml` (`postgres` extra)
- Test: `tests/test_pgvector.py` (create)

**Interfaces:**
- Consumes: `ChunkIndex` (Task 3), `Settings.embed_dim` (Task 4).
- Produces: `PgVectorIndex(db, *, dim: int)` implementing `ChunkIndex` (`query` still raises `NotImplementedError` until Task 6); `Database.engine` property; `kbase.errors.SchemaError`.

- [ ] **Step 1: Add the database and the dependency**

In `docker-compose.yml`, add a profiled service so the default
`docker compose up` stays one SQLite container:

```yaml
  postgres:
    profiles: ["pg"]
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_PASSWORD: kbase
      POSTGRES_USER: kbase
      POSTGRES_DB: kbase
    ports:
      - "5433:5432"
    volumes:
      - kbase-pg:/var/lib/postgresql/data
```

and add `kbase-pg:` under `volumes:`.

In `pyproject.toml`, extend the extra:

```toml
postgres = ["asyncpg>=0.29", "pgvector>=0.3"]
```

Start it and install:

```bash
docker compose --profile pg up -d postgres
.venv/bin/pip install -e ".[dev,postgres]"
export KB_TEST_POSTGRES_URL=postgresql+asyncpg://kbase:kbase@localhost:5433/kbase
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_pgvector.py`:

```python
"""The pgvector backend, held to the same contract as the scan.

Skipped unless KB_TEST_POSTGRES_URL points at a database with the `vector`
extension available -- `docker compose --profile pg up -d postgres`.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from kbase.db import Database
from kbase.models import Base
from kbase.pgindex import PgVectorIndex
from kbase.store import CollectionStore, DocumentStore

POSTGRES_URL = os.environ.get("KB_TEST_POSTGRES_URL", "")

pytestmark = pytest.mark.skipif(
    not POSTGRES_URL, reason="KB_TEST_POSTGRES_URL is not set"
)

DIM = 3


@pytest.fixture
async def db():
    d = Database(POSTGRES_URL)
    async with d.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await d.create_all()
    yield d
    await d.dispose()


@pytest.fixture
async def index(db):
    ix = PgVectorIndex(db, dim=DIM)
    await ix.create_schema()
    return ix


async def _document(db, index, cid, sha):
    docs = DocumentStore(db, index)
    doc, _ = await docs.create(
        cid, title="T", filename="f.md", mime="text/markdown", sha256=sha, data=b"x"
    )
    return docs, doc["id"]


async def test_create_schema_makes_a_vector_column(db, index):
    async with db.session() as s:
        kind = (
            await s.execute(
                text(
                    "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                    "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
                )
            )
        ).scalar_one()
    assert kind == f"vector({DIM})"


async def test_a_written_chunk_reads_back_as_its_vector(db, index):
    cols = CollectionStore(db, index)
    await cols.create("acme", "faq")
    cid = await cols.resolve_id("acme", "faq")
    docs, doc_id = await _document(db, index, cid, "a" * 64)

    await docs.replace_chunks(
        doc_id, cid, [{"ordinal": 0, "text": "hi", "heading": "H", "embedding": [1.0, 0.0, 0.0]}]
    )

    rows = await index.chunks(doc_id)
    assert [r["embedding"] for r in rows] == [[1.0, 0.0, 0.0]]
    assert rows[0]["heading"] == "H"


async def test_deleting_a_document_removes_its_chunks(db, index):
    cols = CollectionStore(db, index)
    await cols.create("acme", "faq")
    cid = await cols.resolve_id("acme", "faq")
    docs, doc_id = await _document(db, index, cid, "b" * 64)
    await docs.replace_chunks(
        doc_id, cid, [{"ordinal": 0, "text": "hi", "heading": "", "embedding": [1.0, 0.0, 0.0]}]
    )

    await docs.delete(doc_id)

    assert await index.chunks(doc_id) == []


async def test_deleting_a_collection_removes_its_chunks(db, index):
    cols = CollectionStore(db, index)
    await cols.create("acme", "faq")
    cid = await cols.resolve_id("acme", "faq")
    docs, doc_id = await _document(db, index, cid, "c" * 64)
    await docs.replace_chunks(
        doc_id, cid, [{"ordinal": 0, "text": "hi", "heading": "", "embedding": [1.0, 0.0, 0.0]}]
    )

    await cols.delete("acme", "faq")

    assert await index.chunks(doc_id) == []


async def test_create_schema_runs_twice_without_losing_the_vectors(db, index):
    """A restart calls this again. It must not be how a corpus disappears."""
    cols = CollectionStore(db, index)
    await cols.create("acme", "faq")
    cid = await cols.resolve_id("acme", "faq")
    docs, doc_id = await _document(db, index, cid, "9" * 64)
    await docs.replace_chunks(
        doc_id, cid, [{"ordinal": 0, "text": "hi", "heading": "", "embedding": [1.0, 0.0, 0.0]}]
    )

    await index.create_schema()

    assert [r["embedding"] for r in await index.chunks(doc_id)] == [[1.0, 0.0, 0.0]]


async def test_a_different_dimension_refuses_to_migrate(db, index):
    from kbase.errors import SchemaError

    with pytest.raises(SchemaError) as caught:
        await PgVectorIndex(db, dim=DIM + 1).create_schema()

    assert str(DIM) in str(caught.value)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_pgvector.py -v`
Expected: FAIL at import — `No module named 'kbase.pgindex'`.

Then unset `KB_TEST_POSTGRES_URL` and run again: expected SKIPPED, not passed.
A test that silently skips in CI is the same as no test, so confirm the skip
reason prints.

- [ ] **Step 4: Expose the engine**

In `src/kbase/db.py`, add:

```python
    @property
    def engine(self):
        """For DDL that SQLAlchemy's metadata cannot express -- the vector
        extension, the typed column, the HNSW index."""
        return self._engine
```

- [ ] **Step 5: Write `src/kbase/pgindex.py`**

```python
"""The pgvector backend.

The ORM never loads a `Chunk` here. `models.Chunk.embedding` is declared `JSON`,
which is what SQLite stores and what `create_all` builds before this class
converts it; a `select(Chunk)` against the converted column would hand
SQLAlchemy a vector literal to JSON-decode. Every statement below is explicit
SQL for that reason, and that constraint is load-bearing -- adding an ORM read
of `Chunk` to this file will fail at runtime, not at import.
"""

from __future__ import annotations

import logging
import math

from sqlalchemy import delete as sa_delete
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from kbase.db import Database
from kbase.errors import SchemaError
from kbase.models import Chunk
from kbase.settings import HNSW_MAX_DIMENSIONS

logger = logging.getLogger(__name__)


def _literal(vec: list[float]) -> str:
    """pgvector's text input form, bound as a parameter and cast in SQL."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


class PgVectorIndex:
    def __init__(self, db: Database, *, dim: int) -> None:
        if dim <= 0:
            raise ValueError("PgVectorIndex needs a positive dimension")
        self._db = db
        # Interpolated into DDL below, so it must be an int and nothing else.
        self._dim = int(dim)

    async def create_schema(self) -> None:
        """Convert the JSON column to `vector(n)`, once.

        The guard is not decoration. Without it a second call adds a fresh
        empty `embedding_vec`, drops the converted `embedding` that holds every
        vector in the corpus, and renames the empty column over it -- silently,
        and on an ordinary restart.
        """
        async with self._db.engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

            existing = (
                await conn.execute(
                    text(
                        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                        "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding' "
                        "AND NOT attisdropped"
                    )
                )
            ).scalar_one_or_none()

            if existing and existing.startswith("vector"):
                if existing != f"vector({self._dim})":
                    # Migrating on a guess would destroy a corpus to satisfy a
                    # typo. The operator restores the setting, or reindexes
                    # deliberately.
                    raise SchemaError(
                        f"chunks.embedding is {existing} but KB_EMBED_DIM is {self._dim}; "
                        "refusing to migrate. Restore the old value, or drop the column "
                        "and reindex."
                    )
                await self._build_index(conn)
                return

            await conn.execute(
                text(
                    f"ALTER TABLE chunks ADD COLUMN IF NOT EXISTS embedding_vec vector({self._dim})"
                )
            )
            # Task 7 adds the backfill and the mismatch handling here. Until
            # then this path is correct only for a database with no chunks in
            # it, which is what this task's tests cover.
            await conn.execute(text("ALTER TABLE chunks DROP COLUMN embedding"))
            await conn.execute(text("ALTER TABLE chunks RENAME COLUMN embedding_vec TO embedding"))
            await self._build_index(conn)

    async def _build_index(self, conn) -> None:
        if self._dim > HNSW_MAX_DIMENSIONS:
            logger.warning(
                "KB_EMBED_DIM is %d; pgvector builds no HNSW index above %d dimensions, "
                "so every search scans. Reduce the model's output width.",
                self._dim,
                HNSW_MAX_DIMENSIONS,
            )
            return
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_chunks_embedding_hnsw "
                "ON chunks USING hnsw (embedding vector_cosine_ops)"
            )
        )

    async def replace(
        self, s: AsyncSession, document_id: str, collection_id: str, rows: list[dict]
    ) -> None:
        await s.execute(
            text("DELETE FROM chunks WHERE document_id = :doc"), {"doc": document_id}
        )
        for r in rows:
            await s.execute(
                text(
                    "INSERT INTO chunks (id, document_id, collection_id, ordinal, text, "
                    "heading, char_count, embedding) VALUES (gen_random_uuid()::text, :doc, "
                    ":col, :ordinal, :text, :heading, :chars, CAST(:emb AS vector))"
                ),
                {
                    "doc": document_id,
                    "col": collection_id,
                    "ordinal": r["ordinal"],
                    "text": r["text"],
                    "heading": r["heading"],
                    "chars": len(r["text"]),
                    "emb": _literal(r["embedding"]),
                },
            )

    async def drop_document(self, s: AsyncSession, document_id: str) -> None:
        await s.execute(text("DELETE FROM chunks WHERE document_id = :doc"), {"doc": document_id})

    async def drop_where_document_in(self, s: AsyncSession, doc_id_select) -> None:
        # A DELETE never selects `embedding`, so the ORM's stale JSON
        # declaration is not consulted and reusing the caller's `select()` here
        # is safe -- unlike a `select(Chunk)`, which is why this file reads
        # every other statement as raw SQL.
        await s.execute(sa_delete(Chunk).where(Chunk.document_id.in_(doc_id_select)))

    async def chunks(self, document_id: str) -> list[dict]:
        async with self._db.session() as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT id, ordinal, text, heading, embedding::text AS emb FROM chunks "
                        "WHERE document_id = :doc ORDER BY ordinal"
                    ),
                    {"doc": document_id},
                )
            ).mappings().all()
        return [
            {
                "id": r["id"],
                "ordinal": r["ordinal"],
                "text": r["text"],
                "heading": r["heading"],
                "embedding": [float(x) for x in r["emb"].strip("[]").split(",") if x],
            }
            for r in rows
        ]

    async def query(
        self, collection_id: str, qvec: list[float], *, limit: int, min_score: float
    ) -> list[dict]:
        raise NotImplementedError  # Task 6
```

- [ ] **Step 6: Add the schema error**

In `src/kbase/errors.py`, following the existing classes' shape:

```python
class SchemaError(KbError):
    """The database's shape and the configuration disagree.

    Never a tenant's problem, so its message is for the log and the operator.
    """
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_pgvector.py -v`
Expected: PASS (6 tests)

- [ ] **Step 8: Run the SQLite suite and commit**

```bash
.venv/bin/pytest -q && .venv/bin/ruff check src tests
git add src/kbase/pgindex.py src/kbase/db.py src/kbase/errors.py \
        docker-compose.yml pyproject.toml tests/test_pgvector.py
git commit -m "feat(pgvector): schema and writes behind the ChunkIndex seam"
```

---

### Task 6: `PgVectorIndex.query`

**Files:**
- Modify: `src/kbase/pgindex.py`
- Test: `tests/test_pgvector.py` (append)

**Interfaces:**
- Consumes: `PgVectorIndex` from Task 5.
- Produces: `query()` returning the same payload shape as `SqlScanIndex.query` — dicts of `text`, `document_id`, `title`, `filename`, `heading`, `score`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_pgvector.py`:

```python
async def _seeded(db, index, rows, tenant="acme", name="faq", sha="d" * 64):
    cols = CollectionStore(db, index)
    await cols.create(tenant, name)
    cid = await cols.resolve_id(tenant, name)
    docs, doc_id = await _document(db, index, cid, sha)
    await docs.replace_chunks(doc_id, cid, rows)
    await docs.mark_indexed(doc_id, len(rows))
    return cid


async def test_query_returns_the_nearer_chunk_first(db, index):
    cid = await _seeded(
        db,
        index,
        [
            {"ordinal": 0, "text": "far", "heading": "", "embedding": [0.0, 1.0, 0.0]},
            {"ordinal": 1, "text": "near", "heading": "", "embedding": [1.0, 0.0, 0.0]},
        ],
    )

    hits = await index.query(cid, [1.0, 0.0, 0.0], limit=5, min_score=0.0)

    assert [h["text"] for h in hits] == ["near", "far"]
    assert hits[0]["title"] == "T"
    assert hits[0]["score"] == pytest.approx(1.0)


async def test_min_score_is_a_floor(db, index):
    cid = await _seeded(
        db,
        index,
        [
            {"ordinal": 0, "text": "orthogonal", "heading": "", "embedding": [0.0, 1.0, 0.0]},
            {"ordinal": 1, "text": "same", "heading": "", "embedding": [1.0, 0.0, 0.0]},
        ],
    )

    hits = await index.query(cid, [1.0, 0.0, 0.0], limit=5, min_score=0.5)

    assert [h["text"] for h in hits] == ["same"]


async def test_query_never_crosses_collections(db, index):
    await _seeded(
        db,
        index,
        [{"ordinal": 0, "text": "secret", "heading": "", "embedding": [1.0, 0.0, 0.0]}],
        tenant="globex",
        name="theirs",
        sha="e" * 64,
    )
    cols = CollectionStore(db, index)
    await cols.create("acme", "mine")
    mine = await cols.resolve_id("acme", "mine")

    assert await index.query(mine, [1.0, 0.0, 0.0], limit=5, min_score=0.0) == []


async def test_a_zero_vector_scores_nothing_rather_than_NaN(db, index):
    cid = await _seeded(
        db,
        index,
        [{"ordinal": 0, "text": "empty", "heading": "", "embedding": [0.0, 0.0, 0.0]}],
        sha="f" * 64,
    )

    # The scan path returns 0.0 for an uncomparable vector; `<=>` returns NaN,
    # and NaN passes a `>= min_score` test in some comparisons. Neither
    # backend may admit it as a hit.
    assert await index.query(cid, [1.0, 0.0, 0.0], limit=5, min_score=0.0) == []


async def test_limit_caps_the_result(db, index):
    cid = await _seeded(
        db,
        index,
        [
            {"ordinal": i, "text": f"c{i}", "heading": "", "embedding": [1.0, 0.0, 0.0]}
            for i in range(5)
        ],
        sha="0" * 64,
    )

    assert len(await index.query(cid, [1.0, 0.0, 0.0], limit=2, min_score=0.0)) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_pgvector.py -v -k "query or floor or zero or limit"`
Expected: FAIL with `NotImplementedError`.

- [ ] **Step 3: Implement `query`**

Replace the `raise NotImplementedError` in `src/kbase/pgindex.py`:

```python
    async def query(
        self, collection_id: str, qvec: list[float], *, limit: int, min_score: float
    ) -> list[dict]:
        """Order in the database, floor in Python.

        Filtering after ordering is equivalent to the scan's floor-then-top-k,
        because the floor is monotonic in distance -- and a `WHERE` on the
        computed score would push the ANN index out of the plan.

        The document join is a second query rather than part of the first: a
        join in the ordered statement gives the planner a reason not to use the
        HNSW index, and this one fetches at most `limit` rows.
        """
        if not any(qvec):
            return []
        async with self._db.session() as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT document_id, text, heading, "
                        "1 - (embedding <=> CAST(:q AS vector)) AS score "
                        "FROM chunks WHERE collection_id = :cid "
                        "ORDER BY embedding <=> CAST(:q AS vector) LIMIT :lim"
                    ),
                    {"q": _literal(qvec), "cid": collection_id, "lim": limit},
                )
            ).mappings().all()

            kept = [
                r for r in rows if not math.isnan(r["score"]) and r["score"] >= min_score
            ]
            if not kept:
                return []

            docs = (
                await s.execute(
                    text("SELECT id, title, filename FROM documents WHERE id = ANY(:ids)"),
                    {"ids": list({r["document_id"] for r in kept})},
                )
            ).mappings().all()
        meta = {d["id"]: d for d in docs}

        return [
            {
                "text": r["text"],
                "document_id": r["document_id"],
                "title": meta[r["document_id"]]["title"],
                "filename": meta[r["document_id"]]["filename"],
                "heading": r["heading"],
                "score": float(r["score"]),
            }
            for r in kept
            if r["document_id"] in meta
        ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_pgvector.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
.venv/bin/ruff check src tests
git add src/kbase/pgindex.py tests/test_pgvector.py
git commit -m "feat(pgvector): distance-ordered search with the floor applied after"
```

---

### Task 7: Carry the vectors already paid for across the migration

**Files:**
- Modify: `src/kbase/pgindex.py` (`create_schema`)
- Test: `tests/test_pgvector.py` (append)

**Interfaces:**
- Consumes: `PgVectorIndex.create_schema` and `SchemaError` from Task 5.
- Produces: a `create_schema` that carries existing vectors across instead of discarding them.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_pgvector.py`:

```python
async def test_existing_json_vectors_are_backfilled_without_re_embedding(db):
    """The JSON column holds vectors someone already paid for."""
    cols_index = PgVectorIndex(db, dim=DIM)  # not yet migrated
    cols = CollectionStore(db, cols_index)
    await cols.create("acme", "faq")
    cid = await cols.resolve_id("acme", "faq")
    async with db.session() as s:
        await s.execute(
            text(
                "INSERT INTO documents (id, collection_id, title, filename, mime, sha256, "
                "bytes_len, data, status, error, chunk_count, created_at) VALUES "
                "('d1', :cid, 'T', 'f.md', 'text/markdown', :sha, 1, '\\x78'::bytea, "
                "'indexed', '', 1, now())"
            ),
            {"cid": cid, "sha": "a" * 64},
        )
        await s.execute(
            text(
                "INSERT INTO chunks (id, document_id, collection_id, ordinal, text, heading, "
                "char_count, embedding) VALUES ('c1', 'd1', :cid, 0, 'hi', '', 2, "
                "'[1.0, 0.0, 0.0]'::json)"
            ),
            {"cid": cid},
        )
        await s.commit()

    await cols_index.create_schema()

    assert [r["embedding"] for r in await cols_index.chunks("d1")] == [[1.0, 0.0, 0.0]]


async def test_a_chunk_of_the_wrong_width_fails_its_document_instead_of_the_boot(db):
    index = PgVectorIndex(db, dim=DIM)
    cols = CollectionStore(db, index)
    await cols.create("acme", "faq")
    cid = await cols.resolve_id("acme", "faq")
    async with db.session() as s:
        await s.execute(
            text(
                "INSERT INTO documents (id, collection_id, title, filename, mime, sha256, "
                "bytes_len, data, status, error, chunk_count, created_at) VALUES "
                "('d2', :cid, 'T', 'f.md', 'text/markdown', :sha, 1, '\\x78'::bytea, "
                "'indexed', '', 1, now())"
            ),
            {"cid": cid, "sha": "b" * 64},
        )
        await s.execute(
            text(
                "INSERT INTO chunks (id, document_id, collection_id, ordinal, text, heading, "
                "char_count, embedding) VALUES ('c2', 'd2', :cid, 0, 'hi', '', 2, "
                "'[1.0, 0.0]'::json)"
            ),
            {"cid": cid},
        )
        await s.commit()

    await index.create_schema()

    doc = await DocumentStore(db, index).get("d2")
    assert doc["status"] == "failed"
    assert "embedding model" in doc["error"]
    assert await index.chunks("d2") == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_pgvector.py -v -k "backfilled or width"`
Expected: FAIL — the JSON vectors are dropped rather than copied, so the first
test finds no chunks and the second finds a document still marked `indexed`.

- [ ] **Step 3: Make `create_schema` migrate**

Everything else in `create_schema` stays as Task 5 wrote it. Only the migrating
branch changes: in `src/kbase/pgindex.py`, replace the placeholder comment and
the bare `DROP COLUMN embedding` that follow the `ADD COLUMN ... embedding_vec`
statement with these four statements, in this order:

```python
            # Copy what is already paid for. No provider call, no spend.
            await conn.execute(
                text(
                    "UPDATE chunks SET embedding_vec = embedding::text::vector "
                    "WHERE embedding_vec IS NULL AND json_array_length(embedding) = :n"
                ),
                {"n": self._dim},
            )
            # What is left was embedded by a different model. Its document is
            # told so in words its tenant can act on, and the bytes are still
            # stored, so `POST /v1/documents/{id}/reindex` is the whole fix.
            await conn.execute(
                text(
                    "UPDATE documents SET status = 'failed', chunk_count = 0, "
                    "indexed_at = NULL, error = :reason WHERE id IN "
                    "(SELECT DISTINCT document_id FROM chunks WHERE embedding_vec IS NULL)"
                ),
                {
                    "reason": "indexed with a different embedding model than the one now "
                    "configured; reindex this document"
                },
            )
            await conn.execute(text("DELETE FROM chunks WHERE embedding_vec IS NULL"))
            await conn.execute(text("ALTER TABLE chunks DROP COLUMN embedding"))
```

The order is load-bearing: the documents are identified by the chunks that
failed to convert, so they must be marked before those chunks are deleted.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_pgvector.py -v`
Expected: PASS (13 tests)

- [ ] **Step 5: Commit**

```bash
.venv/bin/ruff check src tests
git add src/kbase/pgindex.py tests/test_pgvector.py
git commit -m "feat(pgvector): backfill the vectors already paid for, fail the rest"
```

---

### Task 8: Pick the backend, and say so in the README

**Files:**
- Modify: `src/kbase/server/app.py`
- Modify: `README.md`
- Test: `tests/test_backend_selection.py` (create)

**Interfaces:**
- Consumes: everything above.
- Produces: `choose_index(db, settings) -> ChunkIndex` in `src/kbase/index.py`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_backend_selection.py`:

```python
"""Which index a configuration gets, decided in one place."""

from __future__ import annotations

import pytest

from kbase.db import Database
from kbase.index import SqlScanIndex, choose_index
from kbase.settings import Settings


@pytest.fixture
async def db(tmp_path):
    d = Database(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    yield d
    await d.dispose()


def _settings(**env):
    return Settings.from_env(
        {"KB_API_KEYS": "k:acme", "KB_EMBED_BASE_URL": "http://e/v1", "KB_EMBED_MODEL": "m", **env}
    )


async def test_sqlite_always_scans(db):
    s = _settings(KB_DATABASE_URL="sqlite+aiosqlite:///x.db", KB_EMBED_DIM="1536")
    assert isinstance(choose_index(db, s), SqlScanIndex)


async def test_postgres_without_a_dimension_scans(db):
    s = _settings(KB_DATABASE_URL="postgresql+asyncpg://u:p@h/db")
    assert isinstance(choose_index(db, s), SqlScanIndex)


async def test_postgres_with_a_dimension_uses_pgvector(db):
    from kbase.pgindex import PgVectorIndex

    s = _settings(KB_DATABASE_URL="postgresql+asyncpg://u:p@h/db", KB_EMBED_DIM="1536")
    assert isinstance(choose_index(db, s), PgVectorIndex)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_backend_selection.py -v`
Expected: FAIL — `cannot import name 'choose_index'`.

- [ ] **Step 3: Implement the chooser**

At the end of `src/kbase/index.py`:

```python
def choose_index(db: Database, settings) -> ChunkIndex:
    """Postgres plus a dimension means pgvector; anything else scans.

    Making the dimension the switch is what keeps this upgrade non-breaking: a
    Postgres deployment that does not set it keeps the behaviour it has, and
    `kb doctor` is what tells the operator they are still scanning.
    """
    if settings.database_url.startswith("postgresql") and settings.embed_dim > 0:
        from kbase.pgindex import PgVectorIndex

        return PgVectorIndex(db, dim=settings.embed_dim)
    return SqlScanIndex(db)
```

The import is local because `pgindex` needs the `postgres` extra installed, and
a SQLite deployment must not be made to install asyncpg to start.

- [ ] **Step 4: Use it in the app**

In `src/kbase/server/app.py`, replace the `SqlScanIndex(db)` line from Task 3:

```python
        index = choose_index(db, settings)
        await index.create_schema()
        app.state.index = index
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_backend_selection.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Update the README**

In `README.md`, add `KB_EMBED_DIM` to the configuration table:

```
| `KB_EMBED_DIM` | Postgres only. Setting it stores vectors in a pgvector column with an HNSW index; unset means the linear scan. Must match the model's width |
```

Replace the paragraph beginning "Search scores every chunk in the collection"
with:

```markdown
Search has two backends. On SQLite — and on Postgres with no `KB_EMBED_DIM` —
it scores every chunk in the collection, in a worker thread, a partition at a
time: a search costs about 16 MB whether the collection holds a thousand chunks
or a hundred thousand. It is a linear scan, so it slows as the corpus grows,
around 1.2 s over 10,000 chunks.

Set `KB_EMBED_DIM` on Postgres and chunks are stored in a pgvector column with
an HNSW index, and the ordering happens in the database. Vectors already indexed
are copied across on the next start — no re-embedding, no provider spend — and
any chunk whose width does not match marks its document `failed` with a reason,
recoverable with `POST /v1/documents/{id}/reindex`. HNSW does not go above 2000
dimensions; wider than that stores fine and searches by scanning, and `kb
doctor` says so. Results from the two backends are not identical: HNSW is
approximate, which is the ordinary price of a vector index.
```

In "Not here yet", remove nothing — PDF, DOCX, URL crawling, hybrid search and
re-ranking are all still absent.

- [ ] **Step 7: Run everything and commit**

```bash
.venv/bin/pytest -q
KB_TEST_POSTGRES_URL=postgresql+asyncpg://kbase:kbase@localhost:5433/kbase .venv/bin/pytest -q
.venv/bin/ruff check src tests
git add src/kbase tests README.md
git commit -m "feat: choose the index from the configuration, and document both"
```

Both suites must be green: the SQLite run proves nothing regressed, the
Postgres run proves the new backend works.

---

## Deviation from the spec, with reasoning

The spec says `replace` and `mark_indexed` should commit in one transaction.
This plan does not do that, and the reason is that the guarantee is already
there by another route.

`replace` is all-or-nothing, so the window between it and `mark_indexed` holds a
*complete* set of chunks, not a partial one — the thing `indexer.py` was written
to prevent. A crash inside that window leaves chunks under a `pending` document,
and `fail_stale_pending` already deletes exactly those at the next startup.
Forcing one transaction would mean passing a session through `index_document`
into two stores, for a case the existing sweep covers.

If a reviewer disagrees, the change is contained: give `DocumentStore` a
`finish_indexing(document_id, collection_id, rows)` that does both in the
session it already opens, and have `indexer.py` call it instead of the pair.

## What the spec missed

`Base.metadata.create_all` does not add columns to existing tables, so the
denormalized `chunks.collection_id` needed a schema-upgrade step of its own on
**both** backends, not just a pgvector migration. That is Task 1, and without it
any deployment that had indexed a document before this change would fail every
insert after it.
