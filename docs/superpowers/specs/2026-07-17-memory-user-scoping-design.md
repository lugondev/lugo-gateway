# Memory User Scoping — Design

## Problem

Memory is keyed by `profile_id` alone. That was correct when
`2026-07-03-chat-history-memory-design.md:15` chose "Memory: per-profile" — the
system was single-user, so a profile was the only meaningful axis. Two-tier auth
made it wrong, and nobody revisited it.

Two defects follow from the same root:

1. **Cross-user contamination.** On a template profile (`owner_id is None`),
   every user's facts land under one `profile_id`. This is not only a privacy
   leak; it is a correctness bug. B's assistant is told "User thích uống trà"
   because *A* likes tea. B never said it, and the assistant states it
   confidently.
2. **Amnesia on persona switch.** Moving from profile `trợ lý` to `gia sư` drops
   every memory. But "tôi ở Hà Nội" is a property of the person, not the persona.

The schema already anticipated the fix and never wired it: `MemoryItem.user_id`
exists (`services/db/models.py:46`) and is written at `extractor.py:143`, but no
code reads or filters on it. `MemoryProfileDoc.user_id` (`models.py:60`) is never
written at all.

The write itself is the bug: `extractor.py:143` sets `user_id=profile.owner_id`.
For a template that is `None` — *even when the server knows exactly who is
speaking*.

## Decision

Key memory by `(user_id, profile_id)`, with `user_id` as the primary axis and
`profile_id` a secondary partition (so a work persona and a personal persona can
hold different facts about the same person).

`user_id = ''` is the sentinel for "no attributable user" — a device
authenticating with the shared `DEVICE_AUTH_TOKEN`
(`auth_guard.py:165`, the path today's firmware actually uses). That fleet keeps
sharing one bucket per profile, which is the honest meaning of one shared secret.
The bucket disappears on its own once pairing reaches the firmware and devices
carry real per-device identity.

### Why `''` and not `NULL`

- Postgres forbids NULL in a primary key. The repo declares `asyncpg` and
  `psycopg` and treats Postgres as a `DATABASE_URL` swap; a nullable composite PK
  would silently pin memory to SQLite.
- A surrogate PK plus a unique index on `(user_id, profile_id)` fails differently:
  `NULL != NULL` in SQL, so two NULL-user docs both insert and upsert loses
  uniqueness exactly where it matters.
- `''`-as-none is already this codebase's idiom —
  `2026-07-03-chat-history-memory-design.md:27` reads
  `profile_id TEXT -- profile name ('' = no profile)`.

## Changes

### Data

| Table | Change |
|---|---|
| `memories` | No DDL. `user_id` already exists and is indexed. Change what is written; add `user_id` to every filter. |
| `memory_profile_docs` | PK `profile_id` → composite `(user_id, profile_id)`. |

### Identity threading

`user_id` is passed down from the caller instead of inferred by the extractor:

- WS: `SessionConfig.identity_user_id` (`session.py:120`), already resolved from
  `resolve_ws_identity` and already used correctly for the sessions table at
  `session.py:270`.
- HTTP: `current_user_id(request)` in `api/routes/conversation.py`.
- Absent → `''`.

### Signatures

- `extract_and_upsert(session_id, profile, user_id)`
- `MemoryRetriever.get_context(profile, query, user_id)`
- `MemoryStore.list/add/delete_all(profile_id, user_id)`
- compactor and `profile_doc_store` keyed by `(user_id, profile_id)`

### REST

`api/routes/memories.py` scopes every operation to the acting user. Reading a
template profile returns that caller's own memories, not the union.

This also **relaxes** the gate shipped in `tests/unit/test_memory_ownership.py`,
and deliberately so. That fix routed writes through `_can_write`, making a
template's memories admin-only. It was the right call while the bucket was
shared — it limited the blast radius of a leak nobody had scoped away yet. Once
the bucket is per-user, it becomes wrong: it would stop a user from managing
their own memories on a template profile.

Post-scoping both read and write gate on `_visible`. The caller may touch a
profile's memories if they can see the profile at all, because the only bucket
they can reach is their own. `_can_write` keeps governing the profile *config*,
which is genuinely shared; it should never have governed per-user data. The
ownership tests move with the semantics rather than being deleted.

## Migration

There is no Alembic; schema comes from `create_all` at lifespan, which will not
alter an existing table's primary key. A startup migration is required — the
sixth, following the five already registered in `main.py`.

1. `UPDATE memories SET user_id = '' WHERE user_id IS NULL`
2. Rebuild `memory_profile_docs` under the composite PK, mapping every existing
   row to `user_id = ''`.

One-way, idempotent, consistent with the existing migration set.

Measured local state: `memories` holds 10 rows, all `user_id IS NULL`, all on
`esp32-assistant` / `rpi-assistant` — devices on the shared token. They belong in
the `''` bucket already, so no data actually moves. `memory_profile_docs` is
empty locally, but production may hold rows, so the rebuild is
written for real data rather than assumed empty.

## Consequences

Anyone chatting on a template profile starts from empty memory: their old facts
are unattributable and stay in `''`, while they now read their own bucket. This
is visible amnesia and it is the correct outcome — those facts were never
reliably theirs. With current data only the two device profiles are affected, and
they stay in `''`, so nothing observable is lost.

## Testing

- Two users on one template profile keep separate memories (the contamination bug).
- One user across two profiles keeps separate memories (the partition axis).
- Devices on the shared token share the `''` bucket (no regression for the ESP32
  in the field).
- A logged-in user on a template profile is attributed to their own id, not `''`
  — the `extractor.py:143` root cause.
- Migration rewrites NULL → `''` and rebuilds the doc PK without losing content.
- `routes/memories.py` scopes reads and writes to the acting user.
- A non-admin may add and delete their own memories on a template profile
  (the `_can_write` → `_visible` relaxation) while still being unable to see or
  touch another user's bucket on that same profile.

## Out of scope

- Per-fact conflict resolution (ADD/UPDATE/DELETE reconcile).
- Dedup against facts already folded into the profile doc.
- Enabling `embed_model` / `mode: "semantic"`, and the missing similarity floor
  in `_semantic_filter`.
- Scoping memory to a paired device rather than its owner.
- Replacing brute-force cosine with a real vector store.
