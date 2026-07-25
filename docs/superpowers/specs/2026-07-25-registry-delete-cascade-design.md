# Model Registry: clear profile bindings when an entry is deleted or disabled

Date: 2026-07-25

## Problem

A profile pins a concrete registry row: `Profile.stt.(engine, model)`,
`Profile.llm.(engine, model)`, `TtsProfile.(engine, model_id)`. Nothing links the
row's lifecycle back to those bindings, so when an admin removes a row from the
Model Registry the profile keeps pointing at it:

- `check_model_allowed()` (`services/model_registry/gate.py`) rejects the next
  save of that profile with `ModelNotAllowedError` -- the admin sees a profile
  they can't save and no explanation of which row went away.
- At runtime the failure surfaces deeper: `HttpTtsProvider` looks the row up by
  `(kind, engine, model_id)` and fails when it's gone; the TTS-profile picker
  renders the pinned value as `(unavailable)`.

Observed instance: TTS profile `vn-fly` pinned `http_tts/vieneu-fly` after that
Fly deployment was destroyed and its registry row never existed; chatllm profile
`dev-copy` pointed at `vn-fly`. Both had to be repaired by hand.

## Decision

Removing a row (delete) or taking it out of service (disable) **clears every
profile binding that pins exactly that row**, so the profile falls back to the
server default instead of a row that is gone or off.

Blanking, not repointing: there is no safe way to guess which other row an admin
would have chosen, and the fallback path (server default engine / default LLM
row) is already the documented meaning of an empty binding.

## Design

### `app/services/model_registry/cascade.py`

```python
async def clear_bindings_for(kind: str, engine: str, model_id: str) -> list[str]
```

Returns human-readable labels of what it changed (`"dev-copy (stt)"`,
`"vn-cf (tts profile)"`), empty when nothing pinned the row.

| kind | Matches when | Cleared to | Profile then resolves via |
|---|---|---|---|
| `stt` | `Profile.stt.(engine, model)` == row | `engine=""`, `model=""` | `engines.default_stt_engine` |
| `llm` | `Profile.llm.(engine, model)` == row | `engine=""`, `model=""` | the `is_default` llm row |
| `tts` | `TtsProfile.(engine, model_id)` == row | `engine=""`, `model_id=""` | `engines.default_tts_engine` |

Exact-match only. A binding with a blank engine or blank model never matches --
that already means "inherit the default", so there is nothing to clear. Language,
voice mode, ref-audio and every other field on the profile is left untouched.

### Call sites: the routes, deliberately not the store

`model_registry_store.delete()` / `set_fields()` are also called by the
startup migrations -- `migrate_drop_stale_tts_engine_shims()` deletes rows on
every boot. Cascading from inside the store would make each boot rewrite profile
config, exactly the class of silent config loss this repo has already been bitten
by. So the cascade hangs off the two admin-initiated routes only:

- `DELETE /v1/model_registry/{id}` -- after the delete succeeds.
- `PATCH /v1/model_registry/{id}` -- only on an `enabled` transition
  `True -> False`, using the pre-update row to detect the edge (a PATCH that
  re-sends `enabled=False` on an already-disabled row clears nothing).

Both responses gain `"cleared": [...]`; the admin UI prints it in the registry
status line (`Deleted "VieNeu (Fly)" — cleared: vn-fly (tts profile)`).

Because `delete_entry` still requires the row to be disabled first, the disable
step normally does the clearing and the delete branch finds nothing left. It
stays as the safety net for rows deleted through any other path.

### Out of scope (deliberate)

- **No boot-time healing of already-dangling bindings.** Startup runs seeders and
  migrations that create/remove rows; blanking profile config from that ordering
  is how config gets silently lost. Existing dangling bindings are repaired when
  the admin next touches the row or the profile.
- `engines.default_stt_engine` / `default_tts_engine` are engine-level, not row
  bindings -- untouched.
- The `is_default` llm flag: deleting the row removes it with the row; disabling
  leaves the flag set (the response reports it, the admin decides).
- The `(engine, engine)` TTS shim rows: a profile that pins no `model_id` has the
  binding `(engine, "")`, which matches no rule, so it is never cleared.

## Testing

Unit (`tests/unit/test_model_registry_cascade.py`):
- one match per kind, cleared to blank, rest of the profile preserved
- several profiles pinning the same row -> all cleared, all reported
- no match; blank-engine and blank-model bindings never match
- unrelated profiles untouched

Harness prerequisite: `SqliteBackedStore` gains `invalidate()` (same contract as
`ModelRegistryStore.invalidate()`), called from conftest's `_tmp_db` for
`profile_store` / `tts_profile_store` / `mcp_server_store`. Without it these
process-global caches survive the per-test DB switch, so the second test to touch
a config store queries a tmp DB whose tables were never created
("no such table: config_profiles") -- which is why no existing test wrote through
these stores for real.

Route (`tests/unit/test_model_registry_routes.py`):
- disable a pinned row -> binding cleared, response `cleared` names the profile
- enable, and a no-op re-disable -> nothing cleared
- delete an unreferenced row -> `cleared: []`
