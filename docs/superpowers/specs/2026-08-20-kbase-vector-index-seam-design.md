# kbase: a vector index seam, and pgvector behind it

**Status:** approved, not yet implemented
**Scope:** `servers/knowledge-api` only. No gateway work.

## Why

`servers/knowledge-api` scores every chunk in a collection on every search, in
Python, partition by partition (`src/kbase/search.py`). That is the right shape
for the corpus it was built for and it is honest about its ceiling: past 50,000
chunks it logs "move to a vector index" and keeps scanning.

The README already claims the way out exists — *"The interface is the seam a
pgvector implementation slots into later without any caller noticing."* It does
not. `search_collection()` takes a `Database` and writes its own
`select(Chunk, Document)`, and vector writes are spread across five methods of
`store.py`. There is no interface to slot anything into.

This spec builds the seam the README promised, and puts pgvector behind it.

## Decisions

These were settled before design and are not open in the plan:

| Question | Decision |
| --- | --- |
| Does SQLite stay a first-class backend? | Yes. Both backends supported, chosen by `KB_DATABASE_URL` dialect. |
| Where does vector width come from? | An explicit `KB_EMBED_DIM`. Setting it is what turns pgvector on; never probed from the provider. |
| What happens to chunks already indexed? | Backfilled in place from the JSON column. No re-embedding, no provider spend. |
| Must both backends return identical hits? | No. HNSW is approximate; parity tests assert the contract, not per-hit equality. |
| How wide is the seam? | Wide enough for two SQL backends. External stores (Qdrant, Vectorize) are out of scope. |

## Architecture

### The protocol

New module `src/kbase/index.py`:

```python
class ChunkIndex(Protocol):
    async def create_schema(self) -> None
    async def replace(self, document_id: str, collection_id: str, rows: list[dict]) -> bool
    async def drop_document(self, document_id: str) -> None
    async def drop_where_document_in(self, doc_id_select) -> None
    async def query(self, collection_id: str, qvec: list[float], *,
                    limit: int, min_score: float) -> list[dict]
```

Two implementations:

- `SqlScanIndex` — the current numpy partition scan, unchanged in behaviour.
  Serves SQLite, and serves Postgres deployments that have not set
  `KB_EMBED_DIM`.
- `PgVectorIndex` — `vector(n)` column, HNSW, distance ordering in the database.

`Database` selects one from the URL dialect plus settings; `create_app` builds it
once into `app.state.index`, alongside the existing `app.state.db`.

`drop_where_document_in` takes a SQLAlchemy `select()`. This is a deliberate
leak, and the direct consequence of scoping the seam to SQL backends: it
preserves the subquery technique `CollectionStore.delete` uses to stay under the
driver's bind-parameter limit, which a list of ids would breach on an ordinary
corpus. An external store would need a different signature. That is a cost to
pay when an external store is actually built, not before.

### What moves

| From | To |
| --- | --- |
| `store.py:323` `replace_chunks` | `ChunkIndex.replace` |
| `store.py:318` `drop_chunks` | `ChunkIndex.drop_document` |
| `store.py:71` `CollectionStore.delete` chunk deletion | `ChunkIndex.drop_where_document_in` |
| `store.py:284` `DocumentStore.delete` chunk deletion | `ChunkIndex.drop_document` |
| `store.py:368` `fail_stale_pending` chunk deletion | `ChunkIndex.drop_where_document_in` |
| `search.py:100` `search_collection` | `ChunkIndex.query` |

`search.py` keeps `cosine()` and `_score_batch()` as internals of
`SqlScanIndex`. `store.py` keeps every metadata concern and loses every vector
one. `DocumentStore.chunks()` has no caller outside tests, so it moves to the
index without an API consideration.

The embed-the-query step moves out of the index: `routes.py` embeds, then hands
a vector down. An index deals in vectors, not in text and providers — and it
keeps the token accounting on the one path that already reports it.

### A new invariant

**Chunks exist only for documents whose status is `indexed`.**

Today this is nearly true but not guaranteed, so `search` joins `documents` to
filter `status == 'indexed'`. Under ANN that join is expensive in the way that
matters: the filter lives on a different table, so HNSW must over-fetch and
post-filter, and recall degrades exactly when the collection is large enough to
have needed an index.

Three changes make it an invariant:

1. `replace` and `mark_indexed` commit in one transaction.
2. `mark_pending` deletes the document's chunks (today it leaves them).
3. `mark_failed` continues to be preceded by a chunk drop, as `indexer.py`
   already does.

And `chunks` carries a denormalized `collection_id`, immutable for a chunk's
life.

Search then filters on `chunks.collection_id` alone — one table, index usable —
and hydrates `title` / `filename` for the surviving rows (at most `limit`) with
a second query.

**This preserves behaviour.** A document being reindexed is invisible to search
today, because `mark_pending` sets `status = 'pending'` and search filters it
out. After the change it is invisible because its chunks are gone. Same answer,
different mechanism.

### pgvector specifics

- `CREATE EXTENSION IF NOT EXISTS vector`; column `vector(KB_EMBED_DIM)`; HNSW
  index with `vector_cosine_ops`.
- Query is `ORDER BY embedding <=> :q LIMIT :limit`, score `1 - distance`, with
  `min_score` applied in Python afterwards. Filtering after ordering is
  equivalent to the scan's floor-then-top-k, because the floor is monotonic in
  distance.
- **The 2000-dimension ceiling.** pgvector will not build an HNSW index above
  2000 dimensions. `text-embedding-3-large` at 3072 can be stored but not
  indexed. `kb doctor` warns; `create_schema` skips index creation and logs once
  rather than failing to start. The operator's fix is the provider's
  `dimensions` parameter, not a bigger machine.
- A zero-norm vector yields NaN from `<=>`, where the scan path returns 0.0.
  `PgVectorIndex.query` drops NaN rows so both backends agree.

## Migration

Runs inside `PgVectorIndex.create_schema()`, idempotent, safe to re-run:

1. Add `embedding_vec vector(n)`.
2. `UPDATE chunks SET embedding_vec = embedding::text::vector` for rows whose
   JSON array has exactly `n` elements. No provider call, no spend.
3. Rows of a different width: mark the owning document `failed` with a
   tenant-readable reason naming the model change. `POST
   /v1/documents/{id}/reindex` is the documented recovery and re-embeds from the
   bytes already stored.
4. Drop the JSON column, rename, build the HNSW index.

If `KB_EMBED_DIM` disagrees with the width of a column that already exists, the
service **refuses to start** and says so. It does not migrate on a guess: a
silent re-migration would destroy a corpus to satisfy a typo.

SQLite deployments run no migration. The JSON column is the SQLite schema.

## Configuration

| | |
| --- | --- |
| `KB_EMBED_DIM` | Vector width. Required for pgvector; unset means `SqlScanIndex` even on Postgres. |

`Settings.check()` gains: `KB_EMBED_DIM`, when set, must parse as a positive
integer; above 2000 it warns about the HNSW ceiling rather than erroring. On a
Postgres URL with it unset, `kb doctor` says plainly that the service will scan
rather than index — otherwise an operator who deployed Postgres expecting
pgvector gets it silently and never learns why searches are slow.

## Testing

- The existing suite runs unchanged on SQLite. It is the regression net for
  "the seam did not alter behaviour", so it must stay green without edits to
  its assertions — edits there would be evidence the refactor changed something.
- New `tests/test_pgvector.py`, skipped unless `KB_TEST_POSTGRES_URL` is set.
  It asserts the **contract**, not per-hit equality: tenant scoping, `min_score`
  as a floor, `limit` as a cap, deletion by document and by collection, the
  chunk↔indexed invariant, the backfill, and the refuse-to-start on a dimension
  mismatch.
- `docker-compose.yml` gains a `postgres` service on `pgvector/pgvector:pg16`
  behind a compose profile, so the default `docker compose up` stays the
  single SQLite container it is today.

Every test must be shown to fail before its implementation exists. This repo has
a documented history of tests that cannot fail; reasoning about why a test is
sound does not substitute for watching it go red.

## Out of scope

Qdrant, Chroma, and Cloudflare Vectorize. Each needs two-phase write
consistency and a startup reconcile pass that a shared SQL transaction gives
this design for free, plus a `drop_where_document_in` that does not speak
SQLAlchemy. Hybrid keyword search and re-ranking remain where the README left
them.

## Risks

- **Recall is unmeasured.** ANN was accepted without a recall target. If answer
  quality drops after switching a real corpus, the lever is `hnsw.ef_search`,
  and there is no benchmark in the repo to tune it against.
- **The refactor touches every delete path.** An orphaned-chunk bug here is
  invisible: search joins nothing that would surface it, and collection delete
  would not collect it. The invariant tests are the only thing standing there.
