# Gateway memory: compaction into a structured user profile

**Date:** 2026-07-09
**Status:** Design approved (pending written-spec review)
**Area:** `apps/api_gateway/app/services/memory/`

## Problem

The per-profile chat memory (mem0-inspired) has three defects that make it store
too much, unoptimized, "like a conversation log":

1. **(A) Semantic mode is dead — always injects the most-recent 50.** `embed_texts()`
   is only ever called for the *query* in `retriever._semantic_filter`; nothing
   computes an embedding at add-time, so `MemoryItem.embedding` is always NULL.
   In semantic mode `with_vec` is therefore always empty → the code always falls
   back to "return all items". Result: regardless of `mode`, retrieval injects the
   50 newest facts / 2000 chars into every system prompt, unfiltered.

2. **(B) No consolidation — append + exact-string dedup only.** `extract_and_upsert`
   skips a new fact only when `strip().lower()` exactly matches an existing one,
   else appends. Near-duplicate phrasings ("thích tiếng Việt" vs "User prefers
   Vietnamese") and contradictions ("uses PhoWhisper" then "switched to Qwen3-ASR")
   both accumulate forever.

3. **(C) Unbounded store, default `mode="all"`.** Nothing caps the store per profile;
   the default injects everything (up to 50). Over many sessions the store grows
   without bound and old-but-important facts fall off the recency window.

The user also wants: **when memory grows large, a compaction step that recognizes
the user** — collapse many raw facts into a compact identity.

## Approach (chosen)

Raw facts become a **transient buffer**; the user's durable identity lives in a
**structured user-profile document** that an LLM rebuilds on a **size threshold**.
Because compaction keeps the footprint small, the store no longer grows unbounded,
and the profile doc is the "who is this user" block injected each turn.

Rejected alternatives:
- *mem0 per-session reconcile* (one extra LLM call every session): cleaner store
  continuously, but more cost; batched-at-threshold gets the same result cheaper.
- *Merged atomic-fact list / hybrid store*: user chose a structured profile as the
  compacted artifact.

## Data flow

1. **Session end** (`extractor.extract_and_upsert`): one LLM call extracts facts
   (unchanged prompt path). Cheap dedup against existing buffer — cosine ≥
   `dedup_threshold` when `embed_model` is set, else exact `strip().lower()`.
   Surviving facts are added **with their embedding computed** when `embed_model`
   is set (fixes A).
2. **Threshold check**: after adding, if buffer size ≥ `compaction_threshold`
   (default 20) OR ≥ `max_facts` (default 200), invoke the compactor.
3. **Compactor** (`compactor.py`, new — one LLM call): input = current profile doc
   (may be empty) + all buffered raw facts; output = a rebuilt structured profile
   (Markdown sections: Danh tính / Dự án / Sở thích / Ràng buộc / Quan hệ) that
   merges duplicates and resolves contradictions in favor of the newer fact
   (fixes B). On success: upsert the profile doc, then **delete the raw facts that
   were folded in** (keeps footprint bounded — fixes C).
4. **Each chat turn** (`retriever.get_context`): inject = the profile-doc block +
   the remaining raw buffer facts. When `mode="semantic"` and `embed_model` is set
   and the buffer is large, take top-k buffer facts by cosine to the query; else
   include the whole (small) buffer. Whole result is truncated to `MAX_CHARS`.

## Components / changes

| File | Change |
|---|---|
| `services/db/models.py` | New table `MemoryProfileDoc` (`profile_id` PK, `content` Text, `updated_at`). `MemoryItem` unchanged (embedding column already exists). |
| `services/memory/store.py` | New `profile_doc_store` with `get(profile_id) / upsert(profile_id, content) / delete(profile_id)`. New `delete_many(ids)` on `MemoryStore` for pruning folded facts. |
| `services/memory/extractor.py` | Compute embedding at add-time when `embed_model` set; cosine dedup; call compactor when threshold crossed. |
| `services/memory/compactor.py` | **New.** `compact(profile)`: build the LLM prompt, call the profile's LLM, parse the returned Markdown, upsert doc, prune folded facts. Best-effort: swallow+log on failure, never delete facts if the doc write failed. |
| `services/memory/retriever.py` | `get_context` = profile-doc block + buffer (semantic top-k when applicable), truncated to `MAX_CHARS`. Fix the always-fallback path. |
| `services/profiles/models.py` | `MemoryConfig` gains `compaction_threshold: int = 20`, `max_facts: int = 200`, `dedup_threshold: float = 0.92`. |

## Design details

- **Works without `embed_model`** (common for small/local LLM profiles): dedup uses
  exact-string; compaction/merge uses the chat LLM. Embeddings only *improve*
  buffer retrieval when configured.
- **Injection block** keeps the profile doc always-in-full (it is compact by
  construction) plus the small buffer, so current-session facts are visible before
  the next compaction — without reintroducing the unbounded flat list.
- **Compaction atomicity**: fold-then-prune must not lose data — delete raw facts
  only after the doc upsert succeeds, and only the ids that existed at compaction
  start (facts added concurrently survive).
- **Contradiction rule**: the compaction prompt instructs "prefer the more recent
  fact when two conflict"; raw facts are fed newest-last so recency is legible.
- **Backward compatible**: `MemoryProfileDoc` table auto-creates (existing
  `create_all` lifespan); a profile with no doc yet just starts from the buffer,
  and its first compaction folds any pre-existing raw facts.
- **Teardown safety preserved**: extraction and compaction are best-effort; any
  exception is logged and swallowed so session teardown never breaks.

## Testing (TDD)

1. `add` stores a non-null embedding when `embed_model` is configured; null when not.
2. Cosine dedup drops a near-duplicate above `dedup_threshold`; exact-string dedup
   path used when no `embed_model`.
3. Compaction triggers exactly when buffer ≥ `compaction_threshold`; produces a
   profile doc; prunes the folded raw facts; leaves concurrently-added facts.
4. Compaction failure (LLM error / doc write fail) leaves raw facts intact (no data
   loss) and does not raise.
5. `retriever.get_context` injects the profile doc + buffer, respects `MAX_CHARS`,
   and — with `embed_model` set + large buffer + `mode="semantic"` — returns
   query-relevant buffer facts (not just recent).
6. No-`embed_model` end-to-end: extract → dedup → compact → inject all work.

## Out of scope

- No new UI for the profile doc (existing memory list/CRUD endpoints unchanged).
- No cross-profile memory, no time-decay beyond compaction pruning.
- No migration/backfill script — folding happens lazily on first compaction.
