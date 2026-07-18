# Model Registry — one shape for every kind

Date: 2026-07-17
Status: approved, ready for implementation plan

## Problem

The Model Registry has three different shapes for its three kinds, and only one of
them is deliberate.

**STT** enumerates every model variant as its own row. `seed_known_models()` walks
`STT_MODEL_REGISTRIES[engine].list_models()` and creates one row per model id:
`whisper` × 6, `whisper_local` × 6, `qwen3_asr` × 2 (`0.6b`, `1.7b`). The registry
becomes a mirror of a list that already lives in `whisper_models.py`.

**TTS** creates one row per engine, with `model_id` set to the engine's own name —
`tts/vieneu/vieneu`. The model_id carries no information.

**LLM** seeds nothing. An entry *is* the config: you type `openrouter/free` and a
`base_url`, and that's the model.

The LLM shape is the right one, and `gate.py` already says so in its own docstring:

> a (kind, engine, model_id) choice is only restricted if an admin has explicitly
> catalogued it in the model registry. **No matching entry -> unrestricted**,
> preserving today's bring-your-own-endpoint flexibility.

So the gate is opt-in restriction. Rows exist to let an admin *disable* a model or
mark it *testing*. Seeding a row for every known model inverts that: it fills the
registry with rows that restrict nothing, purely to mirror a catalogue.

### What the mirroring actually costs

1. **It manufactures the shadow-row bug.** `seed_known_models()` creates an
   *enabled* `tts/openai_tts/openai_tts` row with `base_url=""`. `openai_tts`
   resolves via `find_enabled(kind, engine)`, so that seeded row is returned ahead
   of any real service entry, and the engine reports *"not configured. Add a Model
   Registry entry with the service's base URL"* — telling the admin to do the thing
   they already did. Verified live: `find_enabled(tts, openai_tts)` returns
   `base_url=''`.

2. **It manufactures a hazard `resolve.py` has to document twice.** Both
   `resolve_stt_engine_config` and `resolve_stt_local_device` carry docstrings
   warning that `find_enabled_sync` "would silently match one of those governance
   rows (empty config) that `seed_known_models()` creates" — so they must use
   `find_sync(kind, engine, "")` instead. The hazard exists only because the seed
   creates those rows.

3. **It leaves orphans behind.** Removing PhoWhisper from the code left its 6 rows
   in the database. The registry's contents drift from the code's reality.

## Decision

**The registry stops being a catalogue mirror. It holds configuration and optional
restriction, nothing else. The catalogue stays in code, where it already lives.**

This is "equal" in the sense that matters: no kind splits rows by version or
parameter count any more.

Input method deliberately stays different, because the kinds differ in nature: a
local model must be *installed* before it can run, so STT/TTS models are chosen
from the installable list; an LLM is a remote endpoint with nothing to install, so
it stays free-form. That asymmetry is in the domain, not in the data model.

## Changes

### `seed.py` — remove both loops

Delete `seed_known_models()` and its call in `main.py`'s lifespan.

The migrations stay exactly as they are. They create the `model_id=""` sentinel
rows that carry real configuration (`default_model`, `device`, `compute_type`,
`model_path`, the OmniVoice group). Verified: `seed_known_models()` only ever
creates `m["id"]` and `engine_name` rows — it never creates a `""` sentinel, so
removing it cannot drop configuration.

Verified that no migration guard collides with the seeded rows: the sentinel
migrations guard on `find("stt", engine, "")` (exact match), and the three
`find_enabled` guards cover `llm`, `whisper_service`, and `eventlab` — none of
which `seed_known_models()` touches. So removing the seed changes no migration's
behavior.

### Stale docstrings

Three comments describe the seed's governance rows as a live hazard and become
wrong once the seed is gone:

- `resolve.py` — `resolve_stt_engine_config` and `resolve_stt_local_device`
- `seed.py` — `migrate_stt_local_models_to_registry` ("distinct from the per-size
  governance rows `seed_known_models()` already creates")

Update them to say the sentinel is simply the engine-level config row. Do not
delete the `find_sync(kind, engine, "")` calls themselves — targeting the sentinel
explicitly is still correct and still the intent.

### Database cleanup

Delete the 27 rows that are pure seed catalogue — `enabled`, `stage="stable"`, no
`base_url`, no `api_key`, empty `config`:

- `stt/whisper/*` × 9 and `stt/whisper_local/*` × 9 (including the 6 leftover
  `phowhisper-*` rows)
- `stt/qwen3_asr/0.6b`, `stt/qwen3_asr/1.7b`
- `tts/<engine>/<engine>` × 7 placeholder rows
- `tts/openai_tts/openai_tts` — the shadow row, currently disabled by hand; once
  the seed is gone nothing recreates it, so delete rather than leave a meaningless
  disabled row

Keep the 15 rows that carry real configuration: the 4 `model_id=""` sentinels, the
OpenRouter rows (they hold `api_key`), the 3 LLM rows, and the 4 local-service
rows.

**This changes no behavior.** All 27 are `enabled` and `stable`, and per `gate.py`
"no matching entry -> unrestricted" — so a deleted permissive row permits exactly
what it permitted before. This is decluttering, not a policy change.

## Explicitly unchanged

- **`routes/profiles.py`** — keeps `registry.validate()`. STT model ids are still
  checked against the installable catalogue at save time, so a typo is still caught
  when the profile is saved rather than mid-conversation. **No new runtime risk.**
- **`whisper_models.py`** — `WHISPER_SIZES` / `_VALID_SIZES` stay. This is the
  installable catalogue and the install button's source of truth.
- **`gate.py`** — unchanged. It already implements the opt-in model this design
  leans on.
- The sentinel-creating migrations.

## Testing

Pin the three properties that matter:

1. `seed_known_models` is **deleted outright** (not kept as a no-op stub) — and a
   fresh database ends up with **only** the migration-created sentinels.
2. The migrations still create their `model_id=""` sentinels with the right config
   on a fresh database, with the seed gone.
3. `check_model_allowed` still blocks a disabled row and a `testing` row for a user
   without `can_use_testing`. **This must not weaken** — it is the whole remaining
   purpose of the registry's restriction path.

Existing tests that assert seeded rows exist must be updated to the new
expectation, not deleted. If a test's intent becomes meaningless, say so rather
than quietly removing it.

## Risks

The admin UI's registry list will show only real configuration on a fresh install,
instead of a pre-populated catalogue. That is the intent, but it is a visible
change: there is nothing to toggle until an admin adds a row to restrict something.

Existing deployments keep whatever rows they already have; the cleanup above is a
one-time manual step against this database, not an automatic migration. A
deployment that wants the same tidy state runs the same deletion.

## Out of scope

- Making LLM pick from a datalist (rejected: it would remove
  bring-your-own-endpoint, which is in active use).
- Removing `registry.validate()` from `profiles.py` (rejected: it is what keeps a
  typo from becoming a runtime failure).
- The `whisper` / `whisper_local` alias — they are the same provider object under
  two names, which is its own duplication, but out of scope here.
