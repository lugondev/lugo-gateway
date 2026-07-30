# Session provenance: which client a conversation belongs to, and what "continue" means

**Date:** 2026-07-30
**Status:** design approved on the two open questions; implementation not started

## Problem

A `sessions` row records `id, profile_id, user_id, created_at, ended_at, meta{stt_engine,
tts_engine}` and nothing about where it came from. A paired device resolves to
`WsIdentity(user_id=device.user_id, device_id=device.id)` (`auth_guard.py:388`), so the
speaker's conversations and their owner's browser conversations are indistinguishable
rows under one `user_id`.

Three clients therefore behave three different ways:

| Client | Remembers its session | Result |
|---|---|---|
| RPi | writes the id to disk (`session_state.py`) and sends it back | resumes its own thread |
| ESP32 | nothing | every wake mints a new session |
| Web | `listSessions(1)` → `rows[0].id` | newest row of the user, **from any client** |

`store.list()` filters on `user_id` (plus optional `profile`) and orders by `created_at
DESC`. Two concrete failures follow:

1. The speaker finishes a conversation; the browser presses Start talking and **continues
   the speaker's thread**, both then writing into one row.
2. The speaker goes idle and wakes: **always a new conversation**, because it remembers no
   id and the server has no notion of "this device's thread".

`ws_session_owner_denied` guards explicit resume, but it compares **users**, not clients —
correct for a deliberate "continue this from History", useless against a "latest" guess.

## Principle

**Implicit resume is scoped per client. Explicit resume stays scoped per user.**

Guessing must never cross clients. Asking (a session id, chosen from History) may, because
that is a person deciding to carry one conversation to another screen.

## Design

### 1. Provenance columns on `sessions`

- `source: str` — `device` | `web` | `api`, extensible by adding values.
- `client_id: str` — the client instance within that source.
  - device → `devices.id`. It lives in the DB, so it survives reboots, NVS wipes and
    reflashes; the thread is not hostage to on-device state.
  - web → `user_id` (decided: one web thread per person, all browsers share it). No client
    change needed, and it already stops the browser from adopting the speaker's thread.
    A per-browser id can be introduced later as a different `client_id` value without
    touching the schema.

Columns, not `meta` JSON: this is a query predicate (`WHERE source=? AND client_id=?
ORDER BY created_at DESC`) on a table headed for Postgres, and JSON predicates do not
index cleanly.

### 2. Implicit resume

On connect with **no** `session_id` from the client, the gateway finds the newest session
for `(source, client_id)` and resumes it: seed history, and `reopen()` it (`ended_at =
NULL`, since it is live again). Nothing found → create a fresh session, which is the
"trừ khi không tìm thấy cuộc hội thoại gần nhất" case.

**No time window** (decided): the client's latest thread is always continued, however long
ago it was. See §4 for what makes that affordable.

Consequences worth stating:
- ESP32 needs **no firmware change** — it already sends no session id.
- RPi keeps working unchanged: an explicit id wins over the lookup.
- Web can drop `listSessions(1)` entirely and let the server resolve its thread.

### 3. Rotation needs no extra flag

`rotate()` creates the new row immediately, so the newest row for the client is always the
freshly opened one. Implicit resume picks it up on the next connect. No `ended_reason`
column, no special case.

### 4. Bounded context on an unbounded thread

`self.history` is currently uncapped: resume seeds **every** message of the session
(`session.py:359`) and each turn sends the whole list to the LLM. `compactor.py` compacts
*memory facts*, not conversations. With no time window, a speaker used daily would replay
one ever-growing transcript on every turn until cost and context window both give out.

So the thread is unbounded but the prompt is not:

- Seed and keep at most the last `conversation_history_max_messages` (decided: **100**)
  messages, dropping oldest first.
- The full transcript stays in the DB — History shows everything; only the prompt is
  trimmed.
- Long-term recall is what the memory system is for: extraction at session end plus
  per-turn retrieval already inject the relevant facts, which is a better answer than
  resending a month of chat.

This is not optional given "no window": without it, choosing unlimited resume chooses
unbounded spend.

### 5. The first message creates the conversation

Today `start()` calls `session_store.create()` on connect, so every connection — every
wake, every page load, every health probe that opens a socket — writes a row whether or not
anyone speaks. Rows with nothing in them are why `rotate()` needs its "an empty session
rotates to itself" rule and why an idle timeout with no turn still litters History.

Lazily instead, the way other chat products do it: **the first stored message creates the
row.**

- The session id is still minted at connect and still reported in `welcome` /
  `session_started` — the wire contract does not change. Only the row is deferred.
- `_persist()` becomes create-on-first-write: ensure the row (with its provenance from §1),
  then append.
- `close()` / `rotate()` skip `mark_ended` and memory extraction when no row was ever
  created. `rotate()`'s condition collapses to "a row exists", which is what `turn > 0` was
  approximating.
- `GET /v1/sessions/{id}` returns 404 for an id that has been announced but not yet
  written to. That matches the model: it is not a conversation until something was said.

**The announcement is the one thing that must not create a row.** The spoken "fresh start"
line (`ConversationSession.announce`) is a stored assistant message, so on its own it would
create exactly the empty-but-for-a-greeting conversation this removes. So a line spoken
before any row exists is held as pending, kept in the in-memory history (the model must
know it already greeted), and flushed into the row — in order, ahead of the user's first
message — if and when a real message creates it. A greeting nobody replied to never becomes
a History entry.

This also cleans up implicit resume from §2: every resumable session now has at least one
real message, so "the client's latest" can never land on a placeholder.

Pre-existing empty rows (this project's DB has a pile of them from today's testing) are a
separate question: deleting them is data deletion and needs an explicit decision, not a
silent migration.

### 6. History gets the distinction the user asked for

`/v1/sessions?source=device|web` (and `client_id`), so History can filter and badge rows
"from the speaker" / "from the web". This is the same data the resume logic needs, exposed.

### 7. Migration

Add both columns as nullable with empty defaults via an idempotent startup migration (the
shape used for the engine-rename migration). Existing rows keep empty provenance and are
therefore **never** implicitly resumed — old data is not retroactively guessed into
somebody's thread.

### 8. Memory: move to Mem0 behind a backend seam

The 100-message window is only half the answer — what falls off the back has to live
somewhere. Today that is a hand-rolled, "mem0-inspired" pipeline (`extractor.py` one LLM
call per session end, `store.py` SQLite rows with JSON embeddings and cosine top-k,
`retriever.py` per-turn injection, `compactor.py` threshold consolidation). The decision is
to use real Mem0 instead.

**Seam, not a rip-out.** Two operations are all the session core actually needs:

```
remember(messages, *, user_id, profile_id, session_id)   # at session end / rotation
recall(query, *, user_id, profile_id, limit) -> list[str] # per turn, injected in the prompt
```

`local` (everything that exists today, untouched) and `mem0` implement that protocol;
`system_config.memory_backend` picks one. Reasons for the seam rather than a replacement:
the current pipeline is metered, quota-gated, user-scoped and covered by tests; a switch
that can be flipped back is how a memory regression stays a config change instead of an
incident. Both implementations fail open, as memory does today — no memory must never mean
no conversation.

**Scope mapping.** Mem0 takes `user_id`, `agent_id`, `run_id`. This project's scoping is
(user, profile, session), so: `user_id` = the project's user id (`""` stays the
shared-device bucket, as now), `agent_id` = `profile_id`, `run_id` = `session_id`.

**The scoping trap, from the docs:** `filters={"user_id": "x"}` matches only memories whose
`agent_id`/`app_id`/`run_id` are all null. Writing with `agent_id` set and reading with a
plain `user_id` filter therefore returns **nothing, silently** — memory that looks like it
works and recalls a blank. Reads must use explicit `{"OR": [...]}` / wildcard filters, and
a test must assert a write with a profile scope is readable by the recall path.

**Cost accounting regresses, and that has to be said out loud.** Mem0 OSS makes its own LLM
and embedding calls through its own provider config, i.e. outside `record_usage` and
`quota_gate`. This project deliberately meters every paid call site and has a harness
(`test_paid_call_site_inventory.py`) that fails when a new one appears unclassified. With
the Mem0 backend, the spend inside `add()`/`search()` is invisible to that ledger: we know
how many calls we made, not what they cost. The inventory row must state that plainly, and
the backend must be switchable off if the cost matters more than the quality.

**Dependency.** `mem0ai` is a new dependency, inert until `memory_backend=mem0` (the pattern
already used for optional engines). OSS mode additionally needs an LLM, an embedder and a
vector store (`qdrant` / `pgvector` / `faiss` / in-memory).

**Open: deployment mode.** OSS self-hosted (own vector store, data stays here) vs Mem0
Platform (`MemoryClient`, API key, data leaves the box). This is an infra/privacy/cost
decision, not a code one — needed before implementation.

## Phasing

1. Provenance + implicit resume + lazy creation + the 100-message cap. Self-contained, fixes
   the reported problem, no new dependency.
2. Mem0 backend behind the seam. Separable, and the seam is worth having even if Mem0 is
   later swapped for something else.

## Known edge, deliberately not solved

Two clients can still end up in one session if a person explicitly continues a device
conversation from History while the device is live in it. Turns would interleave in one
row. Implicit resume can no longer cause this by accident; the explicit case is a person's
choice, and a lock would be a bigger change than the problem justifies today. Revisit if
it actually bites.

## Testing

- Provenance stamped per source: device connect → `source=device, client_id=devices.id`;
  browser connect → `source=web, client_id=user_id`.
- Implicit resume: device reconnects with no id → same session id, history seeded, and the
  row's `ended_at` cleared.
- Cross-client isolation: after a device conversation, a browser connect starts (or
  continues) the **web** thread, never the device's.
- Explicit resume still crosses clients for the owner, and is still denied for a stranger
  (`ws_session_owner_denied` unchanged).
- Rotation then reconnect resumes the fresh row, not the ended one.
- History cap: a session with more messages than the cap seeds only the tail, and the
  stored transcript stays complete.
- Migration: a row written before the columns existed loads, lists, and is never picked by
  implicit resume.
- `/v1/sessions?source=` filters.
- Lazy creation: connecting and saying nothing leaves **no** row; the first user message
  creates it; an announcement spoken before that creates nothing but is flushed into the row
  ahead of the first user message when one arrives; idle-out with no turn leaves History
  untouched.

## Hardware verification

1. Speaker: say something, let it idle out, press Wake → the conversation continues (no
   new History entry).
2. Then open the web client → it does **not** land in the speaker's conversation.
3. Say "bắt đầu lại" to the speaker → new row; wake again → continues the new one.
