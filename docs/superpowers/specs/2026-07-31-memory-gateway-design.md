# Memory Gateway — Contract and Capability Schema

A provider-neutral gateway for AI memory: one library and one API in front of Mem0, Zep,
Letta, Supermemory, or a self-hosted Postgres+pgvector store. This spec covers the **core
contract and capability schema** only. Multi-provider fan-out and the migration engine are
named here so the contract reserves room for them, and specified separately.

## Why this exists

Every memory provider has its own data model, not merely its own API. Mem0 extracts flat
facts scoped by `user_id`/`agent_id`/`run_id`. Zep builds a bi-temporal knowledge graph
where facts have validity intervals. Letta is an agent runtime whose memory blocks are not
separable from the agent. Supermemory is documents plus profiles under a single
`containerTag`. A self-built pgvector store has no extraction step at all.

The consequence is that switching providers is not a config change today, and no gateway
exists that makes it one. Searching the ecosystem (July 2026) turns up framework-level
adapters (Mastra, CrewAI `ExternalMemory`, LangGraph) that are in-process and effectively
single-provider, "universal memory layers" that are pluggable at the *storage* tier but not
the *provider* tier, and Supermemory's Memory Router, which proxies LLM providers while the
memory itself stays Supermemory. The gap is real.

Two concrete goals drive the design:

1. **One library, swappable backend.** A product integrates once and changes provider by
   configuration.
2. **Per-end-user provider diversity.** A product can put end-user A on Mem0, end-user B on
   a self-hosted pgvector store, end-user C on Zep — by tier, price, or data residency.

Goal 1 is served by an embedded library mode. Goal 2 requires shared routing state and is
therefore served by the proxy (gateway) mode. The split is deliberate: routing needs state
that outlives a process, and state that outlives a process is a server.

## What this is not

- Not a memory implementation. The gateway never becomes a better Mem0.
- Not an emulation layer. It does not implement a temporal graph on behalf of a provider
  that lacks one.
- Not a compatibility fiction. Where providers genuinely differ, the difference is
  **declared**, not hidden.

## Decisions

| Decision | Choice | Why |
| --- | --- | --- |
| Positioning | Standalone, provider-neutral product | Owner's call; the platform is one consumer among many |
| Runtime | Python + FastAPI | Provider SDKs are strongest in Python; Mem0 OSS is Python-only in practice |
| Write verbs | `ingest` (raw) + `upsert` (fact) | Providers earn their value in extraction; migration needs the fact path |
| Gateway state | Thin catalog, required; raw journal, opt-in | Stable IDs and bookkeeping without becoming a memory system |
| Missing capability | Reject by default, degrade opt-in | Silent quality loss is worse than a loud 422 |
| Scope | `subject`/`agent`/`session` first-class + free-form `labels` | Validatable, indexable, migratable; matches Mem0 and Zep directly |
| Provider selection | Gateway resolves from a sticky binding | A wrong provider returns an empty recall with no error — the worst failure mode in this system |

## Scope model

Three first-class dimensions, plus open labels:

```python
class Scope(BaseModel):
    subject: str                        # required, non-empty — who the memory is about
    agent: str | None = None            # which persona remembers
    session: str | None = None          # conversation / run
    labels: dict[str, str] = {}         # everything else
```

Mapping:

| Gateway | Mem0 | Zep | Supermemory | pgvector |
| --- | --- | --- | --- | --- |
| `subject` | `user_id` | `user_id` | `containerTag` segment | column |
| `agent` | `agent_id` | `graph_id` | `containerTag` segment | column |
| `session` | `run_id` | `thread_id` | `containerTag` segment | column |
| `labels` | metadata | metadata | metadata | `jsonb` |

**Letta does not map.** Its unit is the agent; there is no independent subject. That is why
Letta is the last adapter, not an MVP one, and why its eventual `Capabilities` will report
`scope_dims: ["agent"]` rather than pretending.

`subject` is **required and non-empty**. The platform's `""`-means-shared-device convention
is deliberately not carried into the product: for a multi-tenant service, an empty subject
is a collision bucket waiting to happen. A caller that wants a shared bucket picks an
explicit reserved value.

### The silent-filter rule

Mem0's documented behaviour: `filters={"user_id": "x"}` matches only memories whose
`agent_id`/`app_id`/`run_id` are all null. Writing with an agent scope and reading with a
plain user filter therefore returns **nothing, with no error**. The rule that follows is
absolute:

> Reads always send a fully-mapped, explicit scope filter. The gateway never forwards a
> partial filter to a provider.

A conformance test asserts that a write carrying an agent scope is readable through the
recall path. This test may not be skipped for any adapter.

## Canonical record

```jsonc
{
  "id": "mg_01JQ...",            // ULID issued by the gateway, stable across providers
  "provider": "mem0",
  "native_id": "abc-123",
  "content": "Prefers black coffee, no sugar",
  "scope": { "subject": "u_1", "agent": "lugo", "session": "s_9", "labels": {} },
  "score": 0.82,                 // search results only
  "created_at": "2026-07-31T04:00:00Z",
  "updated_at": "2026-07-31T04:00:00Z",
  "valid_from": null,            // temporal providers only; null elsewhere
  "valid_to": null,
  "provider_raw": {}             // escape hatch, no schema, never interpreted
}
```

`provider_raw` is intentional. Without it, every provider-specific feature is swallowed by
the abstraction and power users must abandon the gateway. With it, the gateway does not
have to model every feature of every provider in order to stay useful.

## Provider resolution

Resolution order: `scope_binding(tenant, subject)`, then `tenant.default_provider`. If
neither exists, the request fails with `400 invalid_request` (`code:
no_provider_resolved`) — the gateway never picks a provider on the caller's behalf, because
an arbitrary choice here silently strands that end-user's memory in an unexpected backend.

**Only writes create a binding.** Reads resolve but do not bind, so a stray search cannot
pin an end-user to the wrong backend.

The request may carry a `provider` field. It is read as an **assertion, not an
instruction**: if it disagrees with the resolved binding, the gateway returns `409
provider_mismatch` carrying both values. Callers that track provider themselves get a free
integrity check; callers that do not, omit the field. This turns the system's nastiest
failure — ingested to Mem0, retrieved from pgvector, empty result, no exception — into a
loud error.

Changing an end-user's provider is explicit:

```
POST /v1/subjects/{subject}:rebind   { "provider": "zep", "strategy": "fresh_start" }
```

MVP implements `fresh_start` only: bind to the new provider, **leave the old memories in
place at the old provider, delete nothing**, and return
`{"orphaned_at": "mem0", "orphaned_count": 42}`. `strategy: "migrate"` returns `501` until
the migration engine exists. Telling the caller plainly that the end-user will lose their
memories beats pretending the move worked and silently recalling nothing.

## API surface

```
POST   /v1/memories:ingest            raw episode in, provider extracts
POST   /v1/memories:upsert            ready-made facts in            → 501 in MVP
POST   /v1/memories:search
GET    /v1/memories/{gateway_id}
PATCH  /v1/memories/{gateway_id}
DELETE /v1/memories/{gateway_id}
POST   /v1/memories:delete            delete by scope (bulk / erasure)
GET    /v1/subjects/{subject}         current binding
POST   /v1/subjects/{subject}:rebind
GET    /v1/providers                  list + health + capabilities
GET    /v1/capabilities?provider=
GET    /healthz
```

Delete-by-scope is a `POST` because the scope travels in a body and `DELETE` with a body is
unevenly supported across proxies and clients.

`upsert` returns `501` in MVP but is **specified now**: the migration engine requires the
fact path, and adding a verb to a published API later is a breaking change.

There is no `/v1/memory/profile` in the core. A profile is something only Mem0 and
Supermemory offer — it is a **capability**, not a resource. It arrives as a
capability-gated `/v1/profile` after MVP.

### Request shapes

```python
class Message(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str
    at: datetime | None = None

class Episode(BaseModel):
    messages: list[Message] | None = None
    text: str | None = None          # exactly one of messages / text
    metadata: dict = {}

class SearchQuery(BaseModel):
    query: str
    mode: Literal["semantic", "keyword", "hybrid", "graph", "temporal"] = "semantic"
    limit: int = 10
    min_score: float | None = None
    as_of: datetime | None = None    # temporal mode only
    on_unsupported: Literal["reject", "degrade"] = "reject"
    fail_open: bool = False
```

## Capability schema

```python
class Capabilities(BaseModel):
    # write
    supports_ingest: bool
    supports_upsert: bool
    supports_update: bool
    supports_delete: bool
    supports_delete_by_scope: bool
    # read
    search_modes: list[Literal["semantic", "keyword", "hybrid", "graph", "temporal"]]
    supports_score: bool
    max_limit: int
    # scope
    scope_dims: list[Literal["subject", "agent", "session"]]
    supports_labels: bool
    # nature
    memory_model: Literal["flat_facts", "temporal_graph", "memory_blocks", "documents"]
    dedup: Literal["none", "provider", "gateway"]
    # portability
    supports_export: bool
    supports_import: bool
    # operations
    consistency: Literal["read_after_write", "eventual"]
    metered_externally: bool
```

Capabilities are **instance-level, not class-level**. `capabilities()` is a method on a
configured adapter, because configuration changes the answer: Mem0 OSS without a graph
store has no `graph` search mode, and the pgvector adapter without an LLM configured
reports `supports_ingest: false` / `supports_upsert: true` — it can store facts but cannot
extract them.

Three fields carry most of the value and none of them appear in a naive port of a provider
API:

**`consistency`.** Mem0 and Zep process ingestion asynchronously. `ingest()` followed
immediately by `search()` returns nothing. Nobody documents this clearly, and it is the
root cause of flaky integration tests across the whole category. Declaring it is arguably
the single most useful thing the gateway does for a client.

**`metered_externally`.** The provider makes its own LLM and embedding calls inside
`add()`/`search()`, outside any ledger the caller controls. Where true, the caller must
know that the cost figures it sees are incomplete. This cannot be fixed at the gateway
layer, only disclosed.

**`memory_model`.** The field that keeps the schema honest. It states that Zep and Mem0
differ in *kind*, not in feature checklist. Clients that need to know can branch on it;
clients that do not can ignore it.

## Degradation

`on_unsupported` defaults to `reject` (`422 unsupported_capability`). With `degrade`, only
the following substitutions are permitted:

| Requested | Served | `lost` |
| --- | --- | --- |
| `graph` | `semantic` | `["graph_traversal"]` |
| `temporal` | `semantic` + `created_at` filter | `["fact_invalidation"]` |
| `hybrid` | `semantic` | `["keyword_match"]` |

Two things are **never** degraded, even when the client asks: **scope dimensions** and
**delete**. Dropping a scope dimension leaks one end-user's memory into another's results.
Dropping delete violates the right to erasure. Both always return `422`.

> Degradation may reduce **quality**. It may never reduce **isolation** or
> **deletability**.

A degraded response is explicit:

```jsonc
{
  "results": [ ... ],
  "degraded": true,
  "requested": "graph",
  "served": "semantic",
  "lost": ["graph_traversal"]
}
```

## Catalog and journal

```sql
CREATE TABLE memory_index (
  gateway_id   TEXT PRIMARY KEY,        -- ULID
  tenant_id    TEXT NOT NULL,
  provider     TEXT NOT NULL,
  native_id    TEXT NOT NULL,
  subject      TEXT NOT NULL,
  agent        TEXT,
  session      TEXT,
  content_hash TEXT NOT NULL,
  created_at   TIMESTAMP NOT NULL,
  updated_at   TIMESTAMP NOT NULL,
  deleted_at   TIMESTAMP,
  UNIQUE (tenant_id, provider, native_id)
);

CREATE TABLE scope_binding (
  tenant_id     TEXT NOT NULL,
  subject       TEXT NOT NULL,
  provider      TEXT NOT NULL,
  bound_at      TIMESTAMP NOT NULL,
  migrated_from TEXT,
  PRIMARY KEY (tenant_id, subject)
);

CREATE TABLE episode_journal (          -- opt-in per tenant, default OFF
  episode_id  TEXT PRIMARY KEY,
  tenant_id   TEXT NOT NULL,
  subject     TEXT NOT NULL,
  agent       TEXT,
  session     TEXT,
  payload     JSON NOT NULL,
  ingested_to JSON NOT NULL,            -- {provider: [native_id, ...]}
  created_at  TIMESTAMP NOT NULL
);
```

SQLite by default, Postgres via `DATABASE_URL` — the same shape the platform already uses
for its config stores.

The journal is the **only** mechanism that can move an end-user to a new provider when the
old provider has no usable export, which is the common case. It is also why
`supports_export` is a declared capability: with export, migration is a cheap copy; without
it, migration means replaying the journal and paying for extraction again. The journal
holds raw conversation content, so it stays opt-in per tenant and defaults to off — the
tenant makes the privacy-for-portability trade knowingly.

MVP writes the journal. MVP does not read it; the migration engine is a separate spec.

## Adapter contract

```python
class MemoryAdapter(Protocol):
    name: str

    def capabilities(self) -> Capabilities: ...
    async def health(self) -> HealthStatus: ...

    async def ingest(self, episode: Episode, scope: Scope) -> list[ProviderMemory]: ...
    async def upsert(self, facts: list[str], scope: Scope) -> list[ProviderMemory]: ...
    async def search(self, query: SearchQuery, scope: Scope) -> list[ProviderMemory]: ...
    async def get(self, native_id: str) -> ProviderMemory | None: ...
    async def update(self, native_id: str, content: str) -> ProviderMemory: ...
    async def delete(self, native_id: str) -> bool: ...
    async def delete_scope(self, scope: Scope) -> int: ...
```

An adapter never sees a `gateway_id`; it speaks only `native_id`, and the catalog bridges
the two. That boundary is what keeps adapters thin and independently testable, and it is
what allows the catalog to be added, moved, or sharded without touching a single adapter.

`pgvector` is the **first** adapter, ahead of Mem0: it runs offline with no API key, and it
is the baseline that proves the contract can carry a provider that brings no magic of its
own.

## Client library

One core, two modes:

```python
from memgw import Memory

mem = Memory(provider="pgvector", config={...})          # embedded
mem = Memory(base_url="https://...", api_key="...")      # proxy

u = mem.scope(subject="u_1", agent="lugo")
await u.ingest(messages, session="s_9")
hits = await u.search("coffee")                          # across all of u_1's sessions
caps = mem.capabilities()
```

Embedded mode is a single fixed provider with no routing and no deployment. Proxy mode adds
bindings, tenancy, and observability. Routing is not retrofitted into embedded mode.

The scope handle is a client-side convenience only. Callers that already hold a composed
key can split it with `Memory.parse_scope("tnt/u_1/s_9", fmt=...)`, but **the wire format is
always the structured triple**. Collapsing the dimensions into one opaque key would break
three things: cross-session recall (`search(subject=u_1)` with no session is the single
most important memory query, and an adapter can only map a blob onto one native dimension);
tenant isolation (see below); and erasure by subject.

## Authentication and authorization

`tenant_id` is derived from the credential and **never** from the payload. Anything in the
request body may narrow the scope; nothing in it may widen the scope. A payload asserting a
different tenant is `403`, not a silently ignored field. Without this rule, a caller that
composes its own identifiers can read another tenant's memory by crafting one.

MVP: one API key per tenant, backend-to-backend.

Reserved, not built: when the library runs inside an end-user's app (mobile, browser), a
tenant key cannot live there. That needs short-lived subject-scoped tokens minted by the
integrator's backend. The contract already accommodates it — **a `subject` in the token
always wins over a `subject` in the body** — but no token minting ships in MVP.

## Error model

| Status | Code | Meaning |
| --- | --- | --- |
| 400 | `invalid_request` | Malformed body, both/neither of `messages`/`text` |
| 401 | `unauthenticated` | Missing or bad key |
| 403 | `tenant_mismatch` | Payload asserts a tenant other than the credential's |
| 404 | `memory_not_found` | Unknown `gateway_id` for this tenant |
| 409 | `provider_mismatch` | Asserted `provider` disagrees with the binding |
| 422 | `unsupported_capability` | Provider cannot do it and `on_unsupported=reject` |
| 424 | `provider_error` | Upstream provider failed; body carries `retryable` |
| 501 | `not_implemented` | `upsert`, `strategy=migrate` |
| 503 | `provider_unhealthy` | Provider down and `fail_open=false` |

```jsonc
{ "error": { "code": "provider_mismatch", "message": "...",
             "details": { "asserted": "pgvector", "bound": "mem0" } } }
```

Search **fails closed** by default. This is the opposite of the platform's internal memory
seam, where no memory must never mean no conversation — but as a product, a caller that
silently receives zero memories cannot tell an outage from an empty user.

`fail_open: true` restores the permissive behaviour, and does so visibly: `200` with
`{"results": [], "provider_unavailable": true, "provider": "mem0"}`. A caller that opted
into failing open can still distinguish an outage from a genuinely empty user, and can log
it. Writes never fail open — an accepted `ingest` that stored nothing is a lie.

## Conformance suite

A single parametrized test suite runs against every adapter. Writing a new adapter means
passing it. The suite reads `capabilities()` to decide which tests apply — the capability
schema is simultaneously the public API and the thing that drives the tests.

1. Write then read back, with a full scope. *(Catches the silent-filter trap.)*
2. Scope isolation: subject A cannot read subject B's memories. **Never skipped, for any
   capability, on any adapter.**
3. Delete, then search does not return it.
4. `delete_scope` clears its scope and touches no other.
5. Consistency matches the declaration: `read_after_write` must be readable immediately;
   `eventual` polls. Declaring the wrong one fails.
6. `capabilities()` matches real behaviour: a declared `search_mode` must work; an
   undeclared one must raise `Unsupported`.

Test 6 is what stops the capability schema from lying. Self-declared capabilities that
nobody verifies drift out of truth, and a drifted capability is worse than none — clients
branch on it.

## MVP

**In:** client library in both modes · `pgvector` and `mem0` adapters ·
`ingest`/`search`/`get`/`update`/`delete`/`delete_scope` · capabilities endpoint with
strict rejection and opt-in degradation · catalog, `scope_binding`, journal (write-only) ·
`:rebind` with `fresh_start` · conformance suite green on both adapters · generated
OpenAPI · one API key per tenant.

**Out:** `upsert` (`501`) · multi-provider fan-out · read merge and rerank · migration
engine (`501`) · dashboard · Zep, Supermemory, Letta adapters · profile endpoint ·
subject-scoped tokens.

## Phasing

1. This spec: contract, capability schema, catalog, two adapters, conformance suite.
2. Migration engine — export path where `supports_export`, journal replay where not.
   Unlocks `strategy: "migrate"` and `upsert`.
3. Multi-provider fan-out: write-through, read merge, rerank. Requires normalising scores
   across providers, which `supports_score` alone does not solve.
4. Adapters: Zep, then Supermemory, then Letta. Letta last because it is a runtime rather
   than a store and will report a reduced `scope_dims`.

## Known edges, deliberately not solved

- **`fresh_start` loses memory.** Rebinding an end-user before phase 2 leaves their history
  stranded at the old provider. Disclosed in the response, not hidden. Accepted until the
  migration engine lands.
- **Scores are not comparable across providers.** Irrelevant in MVP, which never merges
  results. It is the real blocker for phase 3, and `min_score` is therefore
  provider-relative — documented as such rather than normalised badly.
- **`metered_externally` cannot be fixed here.** When a provider spends inside its own
  `add()`, the gateway knows the call count and not the cost. Disclosure is the whole
  remedy.
- **Eventual consistency is surfaced, not smoothed.** The gateway will not poll on the
  client's behalf; hiding the delay behind a retry loop turns a visible property into an
  invisible latency cost.

## Testing

- Contract: every endpoint against both adapters via the conformance suite.
- Routing: first write binds; reads resolve without binding; a second write to the same
  subject reuses the binding.
- Assertion: `provider` matching the binding passes; disagreeing returns `409` and performs
  no provider call.
- Tenancy: a payload asserting another tenant returns `403`; a `gateway_id` belonging to
  another tenant returns `404`, not `403` (no existence oracle).
- Degradation: `graph` against a flat-fact provider returns `422` under `reject` and a
  `degraded` body under `degrade`; a scope-dimension shortfall returns `422` under **both**.
- Rebind: `fresh_start` changes the binding, leaves old rows intact, reports
  `orphaned_count`; `migrate` returns `501`.
- Journal: off by default; when on, an ingest writes exactly one journal row carrying every
  `native_id` the provider returned.
