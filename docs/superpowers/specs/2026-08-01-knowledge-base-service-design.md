# Knowledge Base Service — Documents, Chunks, Retrieval

A standalone RAG service that turns uploaded documents into retrievable chunks. It lives in
`servers/knowledge-api` as its own repository (a submodule of this one, alongside
`router-memory-services` and `voiceprint-api`), and it is built and shipped **independently
of the gateway**. Nothing in `apps/api_gateway` changes as part of this work.

## Why this exists

The assistant knows two things today: what the profile's system prompt says, and what the
memory subsystem extracted from past conversations. Neither can hold a product manual, an
FAQ, or an internal handbook. `services/memory/` already does embed → cosine → inject, but
it stores single-sentence facts written by an extractor — there is no document, no chunk, no
ingest, and no way for an operator to put a PDF in front of the assistant.

This service supplies the missing half: an operator loads documents into a *collection*, and
any caller with a key can search that collection semantically.

## What this is not

- **Not profile-aware, user-aware, or conversation-aware.** The service knows collections and
  nothing else. Mapping a persona to a collection is the caller's job, and belongs in the
  gateway when that work happens.
- **Not a memory store.** No extraction, no facts, no compaction. Documents in, chunks out.
- **Not a general vector database.** It stores exactly what it chunked, in the shape it
  chunked it.

## Decisions

| Decision | Choice | Why |
| --- | --- | --- |
| Deployment | Standalone service, own repo, own Docker image | Mirrors `memgw`/`voiceprint-api`; can be built and tested with the gateway untouched |
| Who embeds | The service | One round-trip per search for the eventual caller, and ingest is "push the file"; the cost is returned as `usage.prompt_tokens` so a caller can still meter it |
| Store | SQLAlchemy async, SQLite default, Postgres+pgvector as an optional extra | Same posture as `memgw`: nothing to deploy for a small KB, a real index available when one is needed |
| Ingest | Asynchronous — `202 pending`, indexing in the background | Embedding a 200-page PDF takes minutes; a synchronous upload times out before it finishes and leaves the operator with no idea where it broke |
| Scope | `{tenant, collection}` kept as two dimensions | Tenant is stamped from the credential, never read from the body — two tenants both naming a collection `faq` must not read each other's rows |
| Chunk sizing | Characters, not tokens | Tokenizers differ per embedding provider, and Vietnamese diacritics make token estimates swing hard; character counts behave identically everywhere |
| Relevance | `min_score` floor, always applied | Without a floor, top-k always returns *something*, and an assistant will read unrelated content in a confident voice |

## Data model

**Collection** — `(tenant, name)`, unique together. Created explicitly; a document may not be
uploaded into a collection that does not exist.

**Document** — `id`, `collection`, `title`, `filename`, `mime`, `sha256`, `bytes`, `status`
(`pending` | `indexed` | `failed`), `error` (populated only when `failed`), `chunk_count`,
`created_at`, `indexed_at`.

`sha256` is over the raw uploaded bytes. Re-uploading a byte-identical file into the same
collection returns `200` with the existing document rather than `202` and a second copy —
otherwise a
retry after a network blip silently doubles every chunk, and the same passage then wins
top-k twice.

**Chunk** — `id`, `document_id`, `ordinal`, `text`, `heading` (the heading path it was cut
under, e.g. `Bảo hành > Điều kiện đổi trả`), `char_count`, `embedding`.

Chunks are deleted with their document. Deleting a collection deletes its documents.

## HTTP API

Authentication is `Authorization: Bearer <key>`, with `KB_API_KEYS=key:tenant,key:tenant`.
An unset `KB_API_KEYS` means every request is a 401 — the same refusal `memgw` makes, for the
same reason: a service that is reachable and unauthenticated by default gets found.

```
POST   /v1/collections            {name}                    -> 201 {name, document_count}
GET    /v1/collections                                       -> [{name, document_count}]
DELETE /v1/collections/{name}                                -> 204

POST   /v1/documents              multipart: file, collection, title?
                                  or JSON: {collection, title, text}
                                                             -> 202 {id, status: "pending"}
GET    /v1/documents?collection=  &status=                   -> [{id, title, status, ...}]
GET    /v1/documents/{id}                                    -> {id, status, error, chunk_count, ...}
DELETE /v1/documents/{id}                                    -> 204

POST   /v1/search                 {collection, query, limit=5, min_score=0.35}
                                                             -> {chunks: [...], usage: {prompt_tokens}}
GET    /healthz                                              -> 200 (no credential)
```

A search result chunk is `{text, score, document_id, title, filename, heading}`. `title` and
`heading` are carried so a caller can cite a source ("theo mục *Bảo hành*") instead of
quoting a floating paragraph.

CLI, following `memgw`: `kb doctor` checks every configurable value that can be checked, and
`kb serve` refuses to start on a configuration `doctor` already failed.

## Ingest pipeline

1. **Accept and store.** Persist the raw bytes' hash and the document row as `pending`,
   return `202`. The request ends here.
2. **Extract text.** `.txt` / `.md` decode as UTF-8. PDF via `pypdf`, DOCX via
   `python-docx`, both dispatched to a thread pool — parsing someone else's bytes is
   CPU-bound and the most likely thing here to hang or throw.
3. **Chunk.** Markdown splits on `##`/`###` with the heading path retained; any section
   still over the limit is hard-split at ~800 characters with ~100 characters of overlap,
   preferring a sentence boundary within the overlap window.
4. **Embed in batches** of 32 chunks per `/embeddings` call, writing chunks as each batch
   returns.
5. **Finalize** as `indexed` with `chunk_count`.

Failure at any step marks that document `failed` with a readable reason and moves on. One
malformed PDF may not kill the background worker, and it may not take the other queued
documents with it.

A document that fails partway through leaves no chunks behind: the chunks written by earlier
batches are deleted when the document is marked `failed`. A half-indexed document that
reports success is worse than one that reports failure — it answers questions with the first
third of the manual and never says so.

## Search

Embed the query, cosine against the collection's chunks, drop everything under `min_score`,
return the top `limit`. On SQLite this is a scan in Python (the shape `services/memory/`
already uses, adequate to roughly 5–10k chunks); on Postgres it is a pgvector query with an
HNSW index behind the same interface.

`usage.prompt_tokens` reports what the query embedding cost, taken from the provider's own
`usage` block (0 when it reports none).

## Configuration

| | |
| --- | --- |
| `KB_API_KEYS` | `key:tenant,key:tenant`. Unset means every request is a 401 |
| `KB_DATABASE_URL` | SQLite by default; a Postgres URL switches the store |
| `KB_EMBED_BASE_URL` | OpenAI-compatible `/embeddings` endpoint |
| `KB_EMBED_API_KEY` | credential for the above |
| `KB_EMBED_MODEL` | embedding model id |
| `KB_MAX_UPLOAD_BYTES` | rejected above this, before anything is read into memory |
| `KB_DOCS` | `false` closes `/docs` and `/redoc`, which need no credential |

## Testing

The three groups that carry weight:

- **Tenant isolation.** Tenant A's key, searching a collection whose name tenant B also uses,
  returns empty. This is the security test of the service.
- **Index lifecycle.** `pending → indexed` on the happy path; an embedding failure mid-batch
  leaves `failed` with no orphan chunks and never `indexed`; a malformed PDF fails that one
  document while the next queued document still indexes.
- **Chunker.** Heading path preserved, overlap correct, a single 50k-character line
  terminates instead of looping, an empty document produces no chunks.

Plus: auth (no key → 401, wrong key → 401), the `sha256` re-upload path, `min_score`
filtering out a genuinely unrelated query, and upload-size rejection.

## Deployment

`Dockerfile` and `docker-compose.yml` in the service repo. Deployed as its own Coolify app.
Postgres and pgvector only when `KB_DATABASE_URL` points at one.

## Phases

**P1** — collections, text/markdown documents, chunking, embedding, search, auth, CLI,
Docker. Search has to be green before anything heavier lands on top of it.

**P2** — PDF and DOCX extraction, and the document-status polling that a slow ingest needs.
Deliberately second: these are the heaviest dependency and the richest source of malformed
input.

## Not in scope

Gateway integration is designed but explicitly deferred, and no file under `apps/` is touched
by this work. When it happens, the intended shape is: `services/knowledge/{client,retriever}.py`
mirroring `services/memory/`, a `knowledge` block on `Profile` binding a persona to a
collection, retrieval issued concurrently with memory retrieval via `asyncio.gather` at the
two existing injection sites, a character budget separate from memory's `MAX_CHARS`, fail-open
when the service is unreachable, and the new route prefix classified in `core/auth_guard.py`
(which is default-deny).

Also out of scope: URL/website crawling, hybrid keyword+semantic search, re-ranking, and
per-end-user document ownership.
