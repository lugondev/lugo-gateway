# Memory Gateway MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the MVP of `memgw` — one library and one HTTP gateway that fronts a self-hosted pgvector store and Mem0, resolves the provider from a sticky per-subject binding, and declares what each configured provider can and cannot do.

**Architecture:** A provider-neutral core (`types` → `adapters` → `catalog` → `router` → `core`) with two entry points on top: a FastAPI app (proxy mode, multi-tenant, bindings) and a client class (embedded mode, single provider, no server). Adapters speak only `native_id`; the catalog bridges to gateway-issued ULIDs. A parametrized conformance suite runs against every adapter and is driven by each adapter's declared capabilities.

**Tech Stack:** Python ≥3.10, FastAPI, Pydantic v2, SQLAlchemy 2 async (SQLite default, Postgres via `DATABASE_URL`), pytest + pytest-asyncio. `mem0ai` is an optional extra, inert until installed.

**Spec:** `docs/superpowers/specs/2026-07-31-memory-gateway-design.md`

## Global Constraints

- Standalone package rooted at `servers/memory-services/` with its own `pyproject.toml` and its own `tests/`. It must remain extractable to its own repository — **no imports from `apps/` or the parent package**.
- `subject` is required and non-empty. `""` is not a shared bucket here.
- Reads always send a fully-mapped explicit scope filter. Never forward a partial filter to a provider.
- Degradation may reduce quality; it may never reduce isolation or deletability. Scope-dimension and delete shortfalls always return `422`, even under `on_unsupported=degrade`.
- `tenant_id` comes from the credential only. A payload asserting a different tenant is `403`.
- `provider` in a request is an assertion: disagreeing with the binding is `409 provider_mismatch` and **no provider call is made**.
- Writes never fail open. `fail_open` applies to search only.
- `capabilities()` is an instance method — configuration changes the answer.
- Journal defaults to off, per tenant.
- Test command for this package: `cd servers/memory-services && .venv/bin/pytest`. Do not run the parent repo's suite.

---

## File Structure

```
servers/memory-services/
  pyproject.toml
  README.md
  src/memgw/
    __init__.py         # public exports: Memory, Scope, Episode, Message, SearchQuery
    types.py            # Scope, Message, Episode, SearchQuery, ProviderMemory, MemoryRecord, HealthStatus
    capabilities.py     # Capabilities model + SearchMode/MemoryModel literals
    errors.py           # GatewayError hierarchy, code → HTTP status map
    degrade.py          # degradation matrix + resolve_mode()
    adapters/
      __init__.py       # registry: register(), get(), available()
      base.py           # MemoryAdapter Protocol
      pgvector.py       # self-hosted adapter (Postgres+pgvector; SQLite dev fallback)
      mem0.py           # Mem0 adapter, optional dependency
    embedding.py        # Embedder + Extractor protocols used by pgvector.py
    catalog.py          # memory_index / scope_binding / episode_journal + queries
    router.py           # provider resolution + assertion check
    core.py             # MemoryCore: the verbs, over adapter + catalog + router + degrade
    client.py           # Memory: embedded and proxy modes
    server/
      __init__.py
      app.py            # create_app()
      auth.py           # tenant from API key
      schemas.py        # request/response bodies
      routes.py         # /v1/*
  tests/
    conftest.py
    fake.py             # FakeAdapter — in-test reference adapter, validates the suite itself
    test_types.py
    test_degrade.py
    test_catalog.py
    test_router.py
    test_core.py
    test_client.py
    conformance/
      __init__.py
      suite.py          # run_conformance(adapter_factory) — the shared assertions
      test_fake.py
      test_pgvector.py
      test_mem0.py
    server/
      test_auth.py
      test_routes_memories.py
      test_routes_subjects.py
      test_routes_meta.py
```

---

### Task 1: Package skeleton, core types, errors

**Files:**
- Create: `servers/memory-services/pyproject.toml`, `src/memgw/__init__.py`, `src/memgw/types.py`, `src/memgw/errors.py`
- Test: `tests/test_types.py`, `tests/conftest.py`

**Interfaces produced:**
- `Scope(subject: str, agent: str | None, session: str | None, labels: dict[str, str])`
- `Message(role, content, at)`, `Episode(messages, text, metadata)`
- `SearchQuery(query, mode, limit, min_score, as_of, on_unsupported, fail_open)`
- `ProviderMemory(native_id, content, created_at, updated_at, valid_from, valid_to, score, raw)`
- `MemoryRecord(id, provider, native_id, content, scope, score, created_at, updated_at, valid_from, valid_to, provider_raw)`
- `HealthStatus(ok: bool, detail: str | None)`
- `errors.GatewayError(code, message, details, status)` plus subclasses `InvalidRequest`, `Unauthenticated`, `TenantMismatch`, `MemoryNotFound`, `ProviderMismatch`, `UnsupportedCapability`, `ProviderError`, `NotImplementedYet`, `ProviderUnhealthy`

- [ ] **Step 1: Write failing tests** — `tests/test_types.py`: empty `subject` raises; whitespace-only `subject` raises; `Episode` with both `messages` and `text` raises; `Episode` with neither raises; `SearchQuery` defaults are `mode="semantic"`, `on_unsupported="reject"`, `fail_open=False`.
- [ ] **Step 2: Run** `pytest tests/test_types.py -v` → FAIL (module missing).
- [ ] **Step 3: Implement** `pyproject.toml` (name `memgw`, `requires-python = ">=3.10"`, deps `fastapi`, `pydantic>=2`, `sqlalchemy[asyncio]>=2`, `aiosqlite`, `httpx`, `python-ulid`; extras `mem0 = ["mem0ai"]`, `postgres = ["asyncpg","pgvector"]`, `dev = ["pytest","pytest-asyncio","ruff"]`), then `types.py` and `errors.py`.
- [ ] **Step 4: Run** `pytest tests/test_types.py -v` → PASS.
- [ ] **Step 5: Commit** `feat(memgw): scope, episode and error types`

---

### Task 2: Capabilities and the degradation matrix

**Files:**
- Create: `src/memgw/capabilities.py`, `src/memgw/degrade.py`
- Test: `tests/test_degrade.py`

**Interfaces consumed:** `errors.UnsupportedCapability`
**Interfaces produced:**
- `Capabilities` (all fields per spec §Capability schema)
- `degrade.resolve_mode(requested: str, caps: Capabilities, on_unsupported: str) -> DegradeResult`
- `DegradeResult(served: str, degraded: bool, lost: list[str])`
- `degrade.assert_scope_supported(scope: Scope, caps: Capabilities) -> None`
- `degrade.assert_delete_supported(caps: Capabilities) -> None`

The permitted substitutions are exactly: `graph → semantic` (`lost=["graph_traversal"]`), `temporal → semantic` (`lost=["fact_invalidation"]`), `hybrid → semantic` (`lost=["keyword_match"]`). Anything else unsupported is `422` regardless of `on_unsupported`.

- [ ] **Step 1: Write failing tests** — supported mode passes through undegraded; `graph` on a flat-fact provider is `422` under `reject` and `served="semantic", lost=["graph_traversal"]` under `degrade`; `keyword` (no substitution defined) is `422` under **both**; a `Scope` carrying `agent` against `scope_dims=["subject"]` is `422` under **both**; `supports_delete=False` is `422` under **both**.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(memgw): capability schema and the degradation matrix`

---

### Task 3: Adapter protocol, registry, and the conformance suite

**Files:**
- Create: `src/memgw/adapters/base.py`, `src/memgw/adapters/__init__.py`
- Test: `tests/fake.py`, `tests/conformance/suite.py`, `tests/conformance/test_fake.py`

**Interfaces produced:**
- `MemoryAdapter` Protocol with `name`, `capabilities()`, `health()`, `ingest()`, `upsert()`, `search()`, `get()`, `update()`, `delete()`, `delete_scope()` — exact signatures per spec §Adapter contract.
- `adapters.register(name, factory)`, `adapters.get(name)`, `adapters.available() -> list[str]`
- `conformance.suite.run_conformance(make_adapter)` — an async callable running all six checks.

The six conformance checks, from the spec:
1. Write then read back with a full scope (subject+agent+session).
2. Scope isolation: subject A cannot read subject B. **Never skipped.**
3. Delete, then search does not return it.
4. `delete_scope` clears its scope and touches no other.
5. Consistency matches the declaration — `read_after_write` readable immediately, `eventual` polls with a bounded timeout.
6. Declared `search_modes` work; undeclared ones raise `UnsupportedCapability`.

`tests/fake.py` is a dict-backed adapter existing only to prove the suite itself catches failures. It is **not** shipped in `src/`.

- [ ] **Step 1: Write the suite and `FakeAdapter`**, plus `tests/conformance/test_fake.py` running the suite against it.
- [ ] **Step 2: Run** `pytest tests/conformance/test_fake.py -v` → FAIL (protocol missing).
- [ ] **Step 3: Implement** `base.py` and the registry.
- [ ] **Step 4: Run** → PASS. Then deliberately break `FakeAdapter`'s scope filter, confirm check 2 fails, and restore.
- [ ] **Step 5: Commit** `feat(memgw): adapter protocol and a conformance suite that fails liars`

---

### Task 4: Catalog

**Files:**
- Create: `src/memgw/catalog.py`
- Test: `tests/test_catalog.py`

**Interfaces produced:**
- `Catalog(url: str)` with `init()`, and:
  - `record(tenant, provider, native_id, scope, content) -> str` (returns gateway ULID; idempotent on `(tenant, provider, native_id)`)
  - `resolve_gateway_id(tenant, gateway_id) -> CatalogRow | None`
  - `mark_deleted(tenant, gateway_id)`
  - `get_binding(tenant, subject) -> str | None`
  - `bind(tenant, subject, provider) -> None`
  - `rebind(tenant, subject, provider) -> RebindResult(orphaned_at, orphaned_count)`
  - `journal(tenant, scope, payload, ingested_to) -> None`
- Tables exactly as in spec §Catalog and journal.

- [ ] **Step 1: Write failing tests** — `record` twice with the same `native_id` returns the same gateway id; `resolve_gateway_id` for another tenant returns `None` (the route turns that into `404`, not `403`); `bind` then `get_binding` round-trips; `rebind` returns the previous provider and the count of that subject's live rows and leaves those rows intact; `journal` writes one row carrying every native id.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** with SQLAlchemy async; SQLite via `aiosqlite` by default, Postgres when `DATABASE_URL` starts with `postgresql`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(memgw): catalog, bindings and the opt-in episode journal`

---

### Task 5: Router

**Files:**
- Create: `src/memgw/router.py`
- Test: `tests/test_router.py`

**Interfaces consumed:** `Catalog.get_binding`, `errors.ProviderMismatch`, `errors.InvalidRequest`
**Interfaces produced:** `resolve_provider(catalog, tenant, subject, default_provider, asserted) -> str`

Rules: binding wins; else `default_provider`; else `400 no_provider_resolved`. If `asserted` is set and differs from the resolved value → `409 provider_mismatch` with `details={"asserted":..., "bound":...}`.

- [ ] **Step 1: Write failing tests** — binding wins over default; no binding falls back to default; neither → `InvalidRequest(code="no_provider_resolved")`; matching assertion passes; mismatched assertion raises `ProviderMismatch` carrying both values; resolution **never** creates a binding.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement. Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(memgw): provider resolution with assertion checking`

---

### Task 6: Core verbs

**Files:**
- Create: `src/memgw/core.py`
- Test: `tests/test_core.py`

**Interfaces produced:** `MemoryCore(catalog, providers: dict[str, MemoryAdapter], default_provider, journal_enabled)` with `ingest`, `upsert`, `search`, `get`, `update`, `delete`, `delete_scope`, `rebind`, `capabilities`, `providers_status`.

Wiring order for a write: resolve provider → check assertion → capability gate → adapter call → catalog `record` → `bind` (first write only) → journal (if enabled). For a read: resolve (no bind) → `degrade.resolve_mode` → adapter call → map `native_id` to gateway ids → attach `degraded`/`lost`.

`upsert` raises `NotImplementedYet` (`501`). `rebind` accepts `strategy="fresh_start"` only; `"migrate"` raises `NotImplementedYet`.

- [ ] **Step 1: Write failing tests** (against `FakeAdapter`) — first ingest binds, second reuses; search does not bind; search on a dead adapter raises `ProviderUnhealthy` by default and returns `{"results": [], "provider_unavailable": True}` under `fail_open=True`; **ingest on a dead adapter raises even with `fail_open=True`**; `upsert` → `501`; `rebind(fresh_start)` returns `orphaned_at`/`orphaned_count` and leaves rows; `rebind(migrate)` → `501`; journal off writes nothing, on writes one row.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement. Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(memgw): the verbs, wired over adapter, catalog and router`

---

### Task 7: pgvector adapter

**Files:**
- Create: `src/memgw/embedding.py`, `src/memgw/adapters/pgvector.py`
- Test: `tests/conformance/test_pgvector.py`

**Interfaces produced:**
- `embedding.Embedder` Protocol: `embed(texts: list[str]) -> list[list[float]]`
- `embedding.Extractor` Protocol: `extract(episode: Episode) -> list[str]`
- `embedding.cosine(a, b) -> float`
- `PgvectorAdapter(url, embedder, extractor=None)`

Capabilities are computed from configuration: `supports_ingest = extractor is not None`, `supports_upsert = True`, `search_modes = ["semantic"]`, `memory_model = "flat_facts"`, `consistency = "read_after_write"`, `metered_externally = False`, `supports_export = True`, `scope_dims = ["subject","agent","session"]`, `supports_labels = True`.

Storage: one table `memories(native_id, subject, agent, session, labels, content, embedding, created_at, updated_at)`. Vector column is `pgvector` on Postgres and a JSON array scored with `cosine()` in Python on SQLite — the dev path, documented in the README.

- [ ] **Step 1: Write** `tests/conformance/test_pgvector.py` running `run_conformance` against a SQLite-backed adapter with a deterministic fake embedder (hash → fixed-dim vector) and a fake extractor.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement. Step 4: Run** → PASS (all six checks).
- [ ] **Step 5: Add** a test asserting `supports_ingest is False` when no extractor is configured, and that `ingest()` then raises `UnsupportedCapability`. Run → PASS.
- [ ] **Step 6: Commit** `feat(memgw): the pgvector adapter, capabilities derived from its config`

---

### Task 8: Mem0 adapter

**Files:**
- Create: `src/memgw/adapters/mem0.py`
- Test: `tests/conformance/test_mem0.py`

Scope mapping: `subject → user_id`, `agent → agent_id`, `session → run_id`. Reads must send explicit filters covering every dimension that was written — the documented silent-empty trap. Capabilities: `consistency = "eventual"`, `metered_externally = True`, `memory_model = "flat_facts"`, `supports_export = False`, `dedup = "provider"`.

`mem0ai` is optional. The module imports lazily; `adapters.available()` omits `mem0` when the dependency is absent, and the conformance test `skipif`s on it.

- [ ] **Step 1: Write** `tests/conformance/test_mem0.py` — `run_conformance` against Mem0 OSS in in-memory vector-store mode, `skipif` when `mem0ai` is missing. Plus a non-skipped unit test that a search filter built for a scope with `agent` set includes `agent_id` (guards the trap without needing the dependency).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement. Step 4: Run** → PASS or skip cleanly.
- [ ] **Step 5: Commit** `feat(memgw): the mem0 adapter and a guard against its silent-filter trap`

---

### Task 9: FastAPI server

**Files:**
- Create: `src/memgw/server/app.py`, `auth.py`, `schemas.py`, `routes.py`, `__init__.py`
- Test: `tests/server/test_auth.py`, `test_routes_memories.py`, `test_routes_subjects.py`, `test_routes_meta.py`

Endpoints exactly per spec §API surface. `GatewayError` maps to its status via one exception handler; the body is `{"error": {"code", "message", "details"}}`.

- [ ] **Step 1: Write failing tests** — missing key → `401`; body asserting another tenant → `403`; another tenant's `gateway_id` → `404`; asserted provider disagreeing with the binding → `409` **and no adapter call**; `graph` on a flat provider → `422`; `upsert` → `501`; `:rebind` with `migrate` → `501`; `GET /v1/capabilities` returns the configured instance's values; `GET /healthz` → `200`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement. Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(memgw): the HTTP gateway`

---

### Task 10: Client library

**Files:**
- Create: `src/memgw/client.py`; update `src/memgw/__init__.py`
- Test: `tests/test_client.py`

**Interfaces produced:** `Memory(provider=..., config=...)` (embedded) or `Memory(base_url=..., api_key=...)` (proxy); `Memory.scope(subject, agent=None) -> ScopeHandle`; `ScopeHandle.ingest(messages, session=None)`, `.search(query, **kw)`; `Memory.capabilities()`; `Memory.parse_scope(key, fmt)`.

Both modes share `MemoryCore`'s verb names. Embedded mode has no bindings and no tenancy — constructing it with `base_url` **and** `provider` is a `ValueError`.

- [ ] **Step 1: Write failing tests** — embedded round-trips through a real `PgvectorAdapter`; proxy mode issues the right HTTP calls against the FastAPI app via `httpx.ASGITransport`; a `ScopeHandle` search with no `session` reaches the adapter with `session=None` (cross-session recall); `parse_scope("t/u_1/s_9", fmt="{tenant}/{subject}/{session}")` splits correctly; passing both `base_url` and `provider` raises.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement. Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(memgw): one client, embedded or proxy`

---

### Task 11: README and final gate

**Files:**
- Create: `servers/memory-services/README.md`

Covers: what it is, the two modes, provider setup, the SQLite dev fallback for the pgvector adapter, how to add an adapter (implement the Protocol, pass the conformance suite), and the MVP's declared limits (`upsert`/`migrate` are `501`; `fresh_start` strands old memories).

- [ ] **Step 1: Write the README.**
- [ ] **Step 2: Run the whole package suite** `cd servers/memory-services && .venv/bin/pytest -q` → all green.
- [ ] **Step 3: Run** `ruff check src tests` → clean.
- [ ] **Step 4: Commit** `docs(memgw): readme and MVP limits, stated plainly`

---

## Self-Review

**Spec coverage:** scope model → T1; capability schema → T2; degradation → T2; adapter contract + conformance → T3; catalog/binding/journal → T4; provider resolution and assertion → T5; verbs, fail-open asymmetry, rebind → T6; pgvector → T7; mem0 + silent-filter guard → T8; API surface, error model, auth → T9; two-mode client, structured wire, cross-session recall → T10; README + MVP limits → T11. `upsert` and `migrate` are covered as explicit `501`s in T6 and T9.

**Deliberately deferred, per spec §Phasing:** migration engine, multi-provider fan-out, Zep/Supermemory/Letta adapters, dashboard, profile endpoint, subject-scoped tokens.

**Deviation from the skill's default:** task steps carry exact files, interfaces, and test cases rather than full inline source. The plan is executed in the session that wrote it, against a spec committed alongside it; duplicating the implementation into the plan would double the work without adding a reader.
