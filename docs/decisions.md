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

## 2026-08-14 — `servers/router-memory-services` (memgw): adopt embedded, blocked on the shared-device bucket

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

The gateway uses `user_id or ""` as the **shared-device bucket** — memories that
belong to a device rather than a signed-in person. Adopting memgw therefore requires
mapping that bucket onto a real sentinel (`"_shared"`, or the device id if those
memories should stop being shared at all), which is a **data migration**, not a code
swap. Get it wrong and shared-device memories are either lost or merged across
devices, and both failures are quiet.

Scope mapping otherwise is clean: `subject = user_id`, `agent = profile_id`.
memgw's `tenant` dimension has no counterpart here and should be left unset.

**Next step:** decide what the shared-device bucket becomes. Everything else follows
from that.

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
