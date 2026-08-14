# Decisions

Choices that were made deliberately and should not be re-litigated without new
information. Each says what was decided, what it was decided *against*, and what
would change the answer.

---

## 2026-08-14 — SSE `/v1/events` and the in-memory event bus: removed

**Decided:** delete `api/routes/events.py`, `streaming/event_bus.py`, and the
publish/close calls in `routes/stt.py`.

**Against:** keeping it for a future fan-out consumer, or keeping it documented as
deprecated.

**Why.** An endpoint-versus-client sweep across all five first-party clients (admin
console, web SPA, RPi client, ESP32 firmware, `scripts/`) found nothing subscribing
to it. Every one of them reads its events straight off its own WebSocket. The only
tests exercising it were authorization tests — it had an IDOR guard protecting a
feature nobody used, and it carried real session content (partial and final
transcripts) behind that guard.

Unused code that handles sensitive content is a liability, not an asset: it rots
without anyone noticing, and it has to be reasoned about in every future audit.

**What would change it:** an actual consumer that needs fan-out to a party which is
*not* the WebSocket peer — a second dashboard watching a device's session, say.
Build for that consumer rather than restoring this. The replay and terminal-close
semantics in the old bus are the parts worth copying (see `architecture.md`); a
Redis-backed version would be a different implementation anyway.

---

## 2026-08-14 — `GET /v1/stt/models`: removed

**Decided:** delete the route. Keep `STT_MODEL_CATALOGS`, the data behind it.

**Against:** keeping it "for runtime resolution", which is what the Model Registry
design said when it stopped using it for dropdowns.

**Why.** It was the last endpoint with no client. It listed selectable model
variants for local engines, and `GET /v1/model_registry/options?kind=stt`
superseded it: that one covers the same local variants *plus* remote-engine models
an admin registered, and filters by what the caller is allowed to pick. The admin
console already used only the registry endpoint.

`profiles.py` says as much in a comment next to its own use of the catalog —
"it predates the Model Registry options endpoint". The 2026-07-20 registry design
kept the route for "runtime resolution and availability checks", but runtime
resolution never went through HTTP: `apply_stt_model()` and the profile validator
read `STT_MODEL_CATALOGS` directly. So the route was a migration that stopped one
step short.

The catalog itself stays and is still load-bearing — boot warm-up applies a model
through it, and profile save validates variant ids against it.

**What would change it:** a client that needs variant listing without registry
entries. Prefer extending `options` to that, rather than reviving a second answer
to the same question.

---

## 2026-08-14 — `servers/voiceprint-api`: keep vendored, do not wire

**Decided:** leave the submodule in place, do not integrate it into the gateway, and
push the local security patches upstream.

**Against:** wiring it in for speaker identification; or deleting the submodule.

**Why.** This is not first-party code — 27 of its 31 commits are from
xinnan-tech and other upstream authors, it is Apache-2.0, and it predates this
project's use of it. It was built for xiaozhi.

But four commits *are* ours, and they are not cosmetic: P0/P1/P2 fixes for CORS,
audio input validation, connection-pool release, model caching and temp-file
cleanup, with tests. Deleting the submodule would throw those away.

Wiring it in is the wrong direction for a different reason: speaker identification
has no consumer in the product today, so integrating it would add a deploy unit and
an operational surface for a feature nobody has asked for.

The real cost here is that we are carrying security patches for someone else's
project alone. Upstream them. If upstream declines, accept that we own this fork
permanently and say so in its README.

**What would change it:** a product requirement for speaker identification. Then
wire it — and check first whether upstream has moved, since we are several months
behind.

---

## 2026-08-14 — `servers/knowledge-api`: parked; if wired, as an MCP server

**Decided:** leave it standalone. If it is ever integrated, expose it as an **MCP
server** offering a `knowledge_search` tool — not as a `/v1/knowledge` router on the
gateway.

**Against:** building a gateway router for it now.

**Why.** The service is complete and well-tested on its own terms, but nothing in
the product needs per-tenant document RAG yet, and parking it costs nothing: it is a
separate repo that still runs.

The routing decision matters more than the timing. The gateway already has an MCP
registry with tool listing, per-profile attachment, enable/disable and auth. A
knowledge-search tool fits that extension point exactly and adds **zero** new
gateway API surface. A dedicated router would instead mean new auth_guard
classification, new tests, new docs, and a second way of doing what MCP already
does.

**What would change it:** a named use case with a real user. Even then, start with
the MCP shape.

---

## 2026-08-14 — `servers/router-memory-services` (memgw): adopt embedded, blocked on the ownerless bucket

**Decided:** the intended direction is to keep the gateway's public memory API
(`/v1/profiles/{name}/memories`) and replace the internals of `services/memory/`
with memgw in **embedded** mode. Not started, because a data decision has to be made
first.

**Against:** running memgw as a separate service; or leaving two implementations to
diverge.

**Why.** memgw is not a duplicate of the gateway's memory — it is a provider router
(Mem0, Zep, self-hosted pgvector) at 4729 LOC against the gateway's 677, and it has
two modes. Embedded mode is a **library**, not a deployment: its base dependencies
are `pydantic` and `sqlalchemy`, both of which the gateway already has. FastAPI,
mem0, zep and Postgres are optional extras we would not install.

So the adoption cost is far lower than "run another service" suggests, and the
payoff is provider portability plus one implementation instead of two drifting
apart.

**The blocker — read before starting.** memgw's `Scope.subject` validator rejects an
empty string:

```
subject is required and must be non-empty
```

The gateway uses `user_id or ""` for memories with **no identified person** —
still scoped to a profile, just not to anyone. Adopting memgw therefore requires
mapping that bucket onto a real sentinel, which is a **data migration**, not a code
swap. Get it wrong and those memories are either lost or merged into a real
user's, and both failures are quiet. The sentinel is decided in the entry below.

Scope mapping otherwise is clean: `subject = user_id`, `agent = profile_id`.
memgw's `tenant` dimension has no counterpart here and should be left unset.

**Next step:** the migration below. Everything else follows from it.

---

## 2026-08-14 — The ownerless memory bucket: `lugo:anonymous` and `lugo:dev`

**First, what memory is scoped by**, because getting this wrong distorts
everything downstream: the store keys on **`(profile_id, user_id)`** — both
dimensions, on every read and write. A profile is an assistant (its prompt, its
voice, its tools); `user_id` is a person. So:

> A memory belongs to **(which assistant, which person)**.

Many devices sharing one profile is deliberate, not a leak — two speakers in a
house running the same assistant *should* recall the same things. The unit of
separation is the assistant, not the hardware.

**Decided:** replace the single empty-string bucket with **two** reserved
subjects, both meaning "no person identified".

| Today | Becomes | What it holds |
|---|---|---|
| `""` when `identity_unauthenticated` is `False` | `lugo:anonymous` | real speech, from production hardware, where no person was identified — the legacy shared `DEVICE_AUTH_TOKEN` path |
| `""` when `identity_unauthenticated` is `True` | `lugo:dev` | scratch from a local run with auth disabled |

**Against:** one sentinel for both; naming it after the system (`lugo-memory`);
and `lugo:shared-device`, which an earlier draft of this entry chose.

**Why not `lugo:shared-device`.** It names the wrong axis. Sharing is already
expressed by `profile_id` — devices "share" because they run the same assistant,
which is the point. What the subject axis records is *which person*, and the fact
being encoded here is that **there is no person**. `lugo:anonymous` says that;
`lugo:shared-device` implies the device is the unit of scoping, which it is not,
and would mislead the next person into thinking hardware identity lives here.

**Why two, not one.** `session.py` already distinguishes these — a device on the
shared token has `identity_user_id=None` but `identity_unauthenticated=False`,
because it *is* a real authenticated deployment that simply has no owner to
attribute to. Only dev mode is genuinely identity-less. The memory store is the
one place that flattens the distinction, and flattening it puts a laptop's test
utterances in the same bucket as real production speech: one retention policy, one
export, one answer when someone asks what unattributed speech is being held.
Keeping them apart costs one branch on a flag that already exists.

**Why this shape of name.** `subject` answers *whose memories are these*, so the
value has to name a subject. `lugo-memory` names the subsystem instead, which
reads as circular ("these memories belong to memory") and would look like a
username sitting next to real ids.

The three properties that matter:

- **Cannot collide with a real subject.** Real ids are UUIDv4. A colon never
  appears in one, so anything `lugo:`-prefixed is safe by construction rather
  than by luck.
- **Obvious in a raw dump.** Someone reading the table sees `lugo:anonymous`
  next to `1363bc98-40a4-…` and needs no lookup to know which is which.
- **Carries provenance off-box.** This matters specifically because of memgw: in
  proxy mode a subject can be handed to an *external* provider (Mem0, Zep) where
  it sits beside other applications' subjects. A bare `anonymous` is a string
  anyone might use; `lugo:anonymous` is not. memgw's `tenant` dimension would
  normally carry this, but it is stamped from the gateway credential and we leave
  it unset, so the prefix is what actually carries it.

**Migration.** Three parts, and it is only needed if memgw is actually adopted —
`''` works fine for the current in-process store.

1. `services/memory/store.py`'s `_uid()` returns `user_id or ""` today. It becomes
   the sentinel, so new writes land in the right bucket.
2. `UPDATE memories SET user_id = 'lugo:anonymous' WHERE user_id = ''`.
3. The same for **`memory_profile_docs`**, which is easy to miss: it is a second
   table on the same `(user_id, profile_id)` key, holding the compacted
   per-profile document rather than individual facts. Migrating one and not the
   other splits a profile's memory in half — the facts move, the summary does
   not. Note `user_id` is part of that table's **composite primary key**, so this
   is a PK rewrite, not a plain column update.

Idempotent, in the `main.py` lifespan beside the registry migrations. Existing
rows cannot be split between the two sentinels — nothing recorded which case wrote
them — so they all become `lugo:anonymous`, the conservative choice: it treats
existing data as real user speech rather than assuming it was scratch. A
deployment that knows its `''` rows are purely dev data can delete them instead.

**The cost of delaying is not row count.** An `UPDATE` is an `UPDATE` whether it
touches ten rows or a million. What actually accrues is the divergence between the
two memory implementations — that is the reason to decide, not the data volume.

**One consequence worth knowing before it surprises someone.** The migration and
the NULL backfill always assign `lugo:anonymous`, but a *read* with no user
resolves by auth mode. On a box with auth disabled, reads go to `lugo:dev`, so
pre-existing ownerless memories stop being visible there. That is deliberate — dev
mode must not surface production speech — but it does mean a developer's own older
scratch appears to vanish after the first boot on the new code. It is still in the
table under `lugo:anonymous`; setting an admin password makes it readable again.

**A per-device split was considered and rejected**, not deferred. An earlier draft
of this entry left it open as a future privacy improvement. It is not one: giving
each device its own subject would mean two speakers in one house, running one
assistant, stop recalling the same things — the assistant would forget depending
on which speaker you used. Hardware identity does not belong on the subject axis.
If some deployment genuinely needs two devices kept apart, the tool for that
already exists and is the right one: give them different profiles.

---

## 2026-08-14 — Submodule gitlinks: bump only what is pushed

**Decided:** the parent may only record a submodule commit that already exists on
that submodule's remote.

**Why.** A gitlink pointing at an unpushed commit is worse than a stale one. A stale
gitlink gives a fresh clone *older* code; a gitlink naming a commit that exists
nowhere makes `git submodule update` fail outright.

Order is therefore always: push the submodule → bump the gitlink in the parent →
push the parent.

Status when this was written: `knowledge-api` bumped (clean tree, already pushed);
`livehost-api` left stale (10 commits ahead, 1 of them unpushed);
`esp32-assistant` left stale (37 ahead, 9 unpushed, plus uncommitted work in the
tree — an `audio_selftest` component whose host test does pass, so it looks close to
done rather than broken; it needs whoever owns it to call it finished).

---

## 2026-08-14 — Shared profiles are clone-only

`owner_id is None` used to mean both "an admin made it" and "everyone may use
it". Those are now separate: `owner_id` records who made a profile (admins
included), and `Profile.shared` marks a clone-only template.

A shared profile is readable and clonable by everyone and runnable by no one —
including the admin who owns it. That asymmetry is deliberate: a template is a
starting point to copy, not a live configuration that unrelated users and
devices run against, each inheriting an `llm.api_key` and `mcp_servers` they
did not configure.

Legacy ownerless rows are converted on boot
(`services/profiles/shared_migration.py`). A template exactly one live device
owner was running becomes that owner's private profile so deployed fleets keep
working; a template with two or more distinct live owners becomes shared and
logs a WARNING naming the row so an admin can reassign the conflicting
devices.

A template with *no* live bound devices does **not** become shared — that was
the first version of this migration, and it was wrong. Inventorying the real
`data/app.db` found seven ownerless profiles, all with zero live bound
devices, including `esp32-assistant` (66 sessions of history) plus `dev`,
`fast`, and `host` — working assistants, not templates, whose memory and
session history are keyed by profile *name* rather than by any device link.
Sharing them would have silently made every one un-runnable, and cloning to
recover would start empty and orphan that history. So a no-live-devices row
is instead adopted by the first admin (earliest-created user with
`role == "admin"`); if no admin exists yet (a fresh install), the row is left
exactly as it is and re-evaluated on a later boot. The migration now never
creates a shared row on its own — sharing a profile is a deliberate act an
admin performs afterwards with the `shared` checkbox.

Spec: `docs/superpowers/specs/2026-08-14-shared-profile-clone-only-design.md`
