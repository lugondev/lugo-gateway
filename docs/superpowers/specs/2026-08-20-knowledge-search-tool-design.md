# Wiring the knowledge base into the assistant, as a tool

**Status:** approved, not yet implemented
**Scope:** `apps/api_gateway` only. `servers/knowledge-api` is not modified.
**Supersedes:** the deferred-integration sketch in
`2026-08-01-knowledge-base-service-design.md` ("Not in scope"). That sketch
assumed always-inject; this does not. See *Departures* below.

## Why

`servers/knowledge-api` is finished — documents in, retrievable chunks out, now
with a pgvector backend. Nothing calls it. To a user of the assistant the
knowledge base does not exist.

## Decisions

Settled before design; not open in the plan.

| Question | Decision |
| --- | --- |
| When is the knowledge base queried? | When the LLM decides, via a tool it calls. Not injected every turn. |
| Where does the tool live? | In the gateway, as a `ToolSource`. Not an MCP server inside `knowledge-api`. |
| Does the gateway manage documents? | No. Read-only: `/v1/search` and nothing else. Upload and deletion stay with `knowledge-api`'s own API and its `kb` CLI. |

The second decision is the one that costs code, and it was made for metering:
`/v1/search` spends money at an embedding provider on every call, and a tool the
LLM can invoke freely is a new spending path. An MCP server inside
`knowledge-api` would have been nearly free — `Profile.mcp_servers` already
exists — but its spend would be invisible to the gateway's usage recorder. This
repo already has a documented history of metering leaks found by an
anti-omission harness; adding a sixth deliberately is not a trade worth making.

## Architecture

### New files

```
apps/api_gateway/app/services/knowledge/__init__.py
apps/api_gateway/app/services/knowledge/client.py
apps/api_gateway/app/services/conversation/tools/knowledge.py
```

`client.py` holds one `httpx.AsyncClient` for the process and exposes a single
call returning parsed hits plus the reported token usage. One client, not one
per call: this runs inside a conversational turn, and rebuilding the connection
pool per call pays a fresh TCP and TLS handshake on a path where latency is
audible — the same reasoning `kbase`'s own `HttpEmbedder` documents.

`tools/knowledge.py` holds `KnowledgeToolSource`, a `ToolSource` producing
exactly one `Tool`:

- **name:** `search_knowledge`
- **arguments:** `query` (string, required) — and nothing else. The collection,
  `top_k` and `min_score` come from the profile, never from the model. Letting
  the model pick a collection would make cross-persona reads a prompt-injection
  away; letting it pick `top_k` invites it to ask for fifty.
- **returns:** the rendered text block described under *Result shape*.

### Configuration

`SystemConfig` gains three fields, following the `whisper_service_*` precedent
already in that file:

| Field | Meaning |
| --- | --- |
| `knowledge_base_url` | `kbase` service root. Empty disables the tool everywhere. |
| `knowledge_api_key` | Bearer credential. `kbase` maps it to a tenant. |
| `knowledge_timeout_seconds` | HTTP timeout, default 10. |

`Profile` gains a `knowledge: KnowledgeConfig` block:

| Field | Default | Meaning |
| --- | --- | --- |
| `enabled` | `False` | Off unless asked for, so every existing profile behaves exactly as it does today. |
| `collection` | `""` | Which `kbase` collection this persona reads. |
| `description` | `""` | What is in that collection, in the operator's own words. Becomes the tool's description. |
| `top_k` | `5` | Hits requested. |
| `min_score` | `0.35` | Floor, matching `kbase`'s own default. |
| `embed_model` | `""` | Declared for pricing only — see *Metering*. |

The source is added in `session.py`'s registry construction only when
`profile.knowledge.enabled` **and** `knowledge_base_url` is set **and**
`collection` is non-empty. Any of those missing means no tool, not a broken one.

### No new routes

The superseded sketch required classifying a new route prefix in the
default-deny `core/auth_guard.py`. Read-only tool access adds no route, so that
work disappears with it.

## The tool description is the feature

With always-inject the model sees the content whether it wants it or not. With a
tool the model must *choose* to call it, and it chooses from the description
alone. `"Search the knowledge base"` produces one of two failures: a model that
calls it on every "cảm ơn", or a model that never calls it when it matters.

So `KnowledgeConfig.description` is written by the operator, names the actual
subject matter, and becomes the tool's description verbatim — *"Tra cứu sổ tay
bảo hành, chính sách đổi trả và hướng dẫn lắp đặt của cửa hàng"*, not a generic
label. A deployment that leaves it blank gets a usable but generic fallback
naming the collection, and `kb doctor`-style guidance is out of scope here; the
admin UI field help is where this gets said.

This is the highest-leverage decision in the design, and it is a content
decision, not a code one.

## Metering, and a seam that can drift

`record_usage` resolves price from the Model Registry by `(kind, engine,
model_id)`. Its own source comment warns that a blank `model_id` cannot match
the row carrying the price, "so it would silently cost $0 forever".

The gateway cannot observe which model `kbase` used: the embedding happens
inside that service under its own `KB_EMBED_MODEL`. `/v1/search` returns
`usage.prompt_tokens` but does not name the model.

Therefore `KnowledgeConfig.embed_model` is **declared** by the operator and must
match `kbase`'s `KB_EMBED_MODEL`. Usage is recorded with `kind="embed"` and that
model id, after a successful call, never before.

This is a real seam that can drift: change `KB_EMBED_MODEL` and forget the
profile, and the cost is priced against the wrong row. Accepted here rather than
solved, because the alternative — extending `/v1/search` to report its model —
changes a service this work is scoped not to touch. It is the first thing to fix
if the two ever disagree in practice.

## Failure behaviour

Fail-open, and quiet about infrastructure.

An unreachable service, a timeout, a non-200, or a malformed body all produce a
short sentence back to the model saying the lookup was not possible — never an
exception. A tool that raises kills the turn, which is a worse outcome than an
assistant that answers without the document.

The message must not carry the base URL, the driver error, or the status body.
`kbase` keeps deployment detail out of tenant-visible fields for the same
reason; a tool result is read by an LLM and may be spoken aloud.

Knowledge is **not** added to the profile health-check gate that runs on
WebSocket connect. Fail-open and connect-blocking are contradictory.

## Result shape

The `Tool` contract returns a string. Each hit is rendered with its heading path
so the model can attribute an answer — `Sổ tay > Bảo hành > Đổi trả` — followed
by the chunk text.

The rendered block has its own character budget (2000, matching memory's
`MAX_CHARS` by coincidence of scale rather than by sharing it) and is truncated
at a line boundary, never mid-line. Memory's budget is untouched: these are
different injection paths and must not compete.

Zero hits is a normal outcome, not a failure, and says so plainly — a model told
"no matching documents" answers better than one handed an empty string.

## Testing

- `client.py` against `httpx.MockTransport`: parses hits and usage, maps a
  non-200 and a timeout to the fail-open path, sends the bearer credential.
- `tools/knowledge.py`: renders hits with heading paths, respects the character
  budget, returns the no-hits sentence, fails open without leaking the URL or
  the driver error, and records usage exactly once on success and not at all on
  failure.
- Wiring: the source appears only with `enabled` + `base_url` + `collection`,
  and a profile with no `knowledge` block at all behaves exactly as before.
- The description reaches the tool schema verbatim — a truncated or replaced
  description is the failure mode that makes the whole feature silently useless.

Every test must be observed failing before its implementation exists. This repo
has a documented history of tests that cannot fail; reasoning about why a test
is sound does not substitute for watching it go red.

## Departures from the superseded sketch

The 2026-08-01 spec assumed retrieval injected into the system prompt every
turn, concurrent with memory via `asyncio.gather` at the two existing injection
sites, with a character budget carved out beside memory's. None of that is built
here. The two injection sites are untouched, nothing is gathered, and the two
budgets never meet. What survives from that sketch is the file layout mirroring
`services/memory/`, the profile-level collection binding, and fail-open.

## Out of scope

Document management through the gateway, an admin UI for it, and the route
classification that would need. Multi-collection search from one profile.
Letting the model choose the collection. Re-ranking. Citations rendered as
structured data rather than text.
