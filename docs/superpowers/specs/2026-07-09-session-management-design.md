# Chat session management

**Date:** 2026-07-09
**Status:** Approved

## Problem

The sessions panel lists chat sessions and loads one on click, but offers no way
to delete them. Empty and stale sessions accumulate. Users need to delete a single
session, delete several selected at once, clear all empty sessions, and clear all
sessions.

## Scoping principle

Every bulk / clear action mirrors what the panel is currently showing — the same
`profile` filter the list endpoint uses:

- A profile is selected → act only on that profile's sessions.
- No profile selected → the panel lists everything, so the action covers everything.

What you see is what you delete. No hidden global wipes.

## Backend — store (`services/history/store.py`)

Add two methods to `SessionStore`:

- `delete_many(ids: list[str]) -> int` — delete the given sessions and their
  messages; return the number actually deleted (missing IDs are skipped, not errors).
  Empty list → returns 0, no-op.
- `clear(profile_id: str | None, only_empty: bool = False) -> int` — delete sessions
  in scope and their messages; return the count deleted.
  - `profile_id is None` → all sessions (matches `list(profile_id=None)`).
  - `profile_id == "x"` → only that profile's sessions.
  - `only_empty=True` → restrict to sessions with zero messages.

Both delete child `ChatMessage` rows first (same order as the existing `delete`).

## Backend — routes (`api/routes/sessions.py`)

- `DELETE /v1/sessions/{id}` — *(exists)* single delete.
- `POST /v1/sessions/bulk_delete` — body `{"ids": ["a","b"]}` → `{deleted: n}`.
  Empty/absent list → `{deleted: 0}`.
- `DELETE /v1/sessions?profile=X&only_empty=true` — clear empty in scope → `{deleted: n}`.
- `DELETE /v1/sessions?profile=X` — clear all in scope → `{deleted: n}`.

`profile` is optional on the clear route; when omitted, scope is all sessions.
`only_empty` defaults to false.

## UI (`static/js/sessions.js` + `static/index.html`)

- Each session row: a checkbox (stop click-to-load propagation) + a per-row ✕ delete
  button (calls `DELETE /v1/sessions/{id}`).
- Panel toolbar (in the session panel header): **Delete selected** (disabled until
  ≥1 checkbox is checked), **Clear empty**, **Clear all**.
  - Delete selected → `POST /v1/sessions/bulk_delete` with the checked IDs.
  - Clear empty → `DELETE /v1/sessions?...&only_empty=true`, passing the panel's
    current `profile` filter.
  - Clear all → `DELETE /v1/sessions?...` with the current `profile` filter.
- `confirm()` guard on Clear empty and Clear all (Clear all names the scope).
- After any successful delete: re-render the panel list. If the currently open chat
  session was removed, reset to a fresh session (clear `currentSessionId`, dialogue,
  and `chat.history`).

## Tests (TDD)

Store (`tests/unit/test_session_store.py`):
- `delete_many` returns the count of existing IDs deleted; missing IDs skipped;
  empty list → 0; messages of deleted sessions gone.
- `clear(only_empty=True)` deletes only zero-message sessions; keeps non-empty ones.
- `clear` all-in-scope respects `profile_id`; `profile_id=None` clears everything.

Routes (`tests/unit/test_sessions_routes.py`):
- `POST /v1/sessions/bulk_delete` deletes listed sessions, returns count; empty list → 0.
- `DELETE /v1/sessions?only_empty=true&profile=X` clears empty in scope, returns count.
- `DELETE /v1/sessions?profile=X` clears all in scope; other profiles untouched.

UI has no JS test harness in this repo; verified manually by driving the panel.
