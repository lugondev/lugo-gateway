# System Settings Restructure — Design

## Goal

The admin "System settings" page (`#subtab-system-settings` in the static admin
console) currently dumps 4 flat groups / 42 raw Pydantic field names onto the
page with zero styling, zero descriptions, and one shared Save button. Two
problems, tackled together:

1. **Presentation**: fields render as `<label>{raw_snake_case_name}</label>`
   inside unstyled `<details>` accordions — no grouping logic within a group,
   no help text, no unit hints, wrong input type for at least one field
   (`conversation_system_prompt` is a multi-sentence prompt rendered as a
   single-line `<input type="text">`).
2. **Scope**: several fields are deployment-time constants (file paths, a
   binary path, a download URL, a fixed sample rate, startup-only flags, a
   secret token) that were pulled into the admin-editable `SystemConfig`
   during an earlier "79 env → SQLite" migration, but are read once at
   process startup/init and never meaningfully "tuned" at runtime. They add
   noise to a page meant for live tuning and, for the secret
   (`pyannote_auth_token`), sit somewhere they arguably shouldn't (an
   app-owned SQLite row) regardless of change frequency.

This spec restructures the page so **only fields an operator plausibly
changes without a redeploy stay in the admin UI**; everything else moves to
env-var-backed settings with code defaults.

## Non-goals

- No change to the storage mechanism for fields that stay admin-editable
  (still one `SystemConfig` JSON blob in `config_system` SQLite row, PUT
  deep-merge via `/v1/system/config`).
- No deep-nesting of Pydantic fields (rejected after blast-radius research:
  ~30 call sites across the codebase do flat 2-hop attribute access like
  `cfg.conversation.conversation_barge_in_grace_ms`; nesting would break all
  of them for a purely cosmetic grouping need — see "Field metadata" below
  for how sub-grouping is achieved without this).
- No change to the Base Context or Status subtabs (siblings of Settings,
  out of scope).
- This spec's scope grew beyond the Settings page itself: removing the
  `conversation_stt_engine`/`conversation_tts_engine`/query-param override
  tier (user-requested simplification, see below) also requires removing
  the STT engine-select dropdown from the Chat and Livehost pages and
  updating device-integration docs — flagged explicitly rather than treated
  as incidental.

## Field classification

Every field currently in `EngineDefaults`, `SttLocalConfig`,
`ConversationTuningConfig`, `PreprocessingConfig` (in
`apps/api_gateway/app/services/system_config.py`), classified by actual
read pattern (confirmed via grep, not guessed):

### Move to env var (`app/core/settings.py`), remove from `SystemConfig`

| Current group | Field | Current default | Why | Call sites to update |
|---|---|---|---|---|
| engines | `ollama_bin` | `""` | binary path, read once (capability check + subprocess spawn) | `llm_models.py:34`, `recommend/capabilities.py:122` |
| engines | `warmup_on_startup` | `True` | read once at process startup | `main.py:157` |
| engines | `warmup_startup_timeout_s` | `180` | read once at process startup | `main.py:159,163` |
| stt_local | `stt_model_dir` | `"models/stt"` | download dir path, read once at `ModelManager.__init__` | `models.py:37`, `recommend/capabilities.py:172` |
| stt_local | `vosk_model_base_url` | `"https://alphacephei.com/vosk/models"` | fixed download URL | `models.py:94` |
| stt_local | `stt_stream_sample_rate` | `16000` | fixed audio pipeline constant | `system.py:80`, `conversation.py:248`, `lugo.py:109`, `livehost.py:132`, `stt.py:169` |
| stt_local | `stt_glossary_path` | `""` | file path (not content), deploy-specific | `stt/providers/whisper_mlx_provider.py:59`, `whisper_provider.py:99` |
| preprocessing | `pyannote_vad_model` | `"pyannote/segmentation-3.0"` | model id, chosen once when VAD backend is set up | `services/vad.py:84`; cache-diff logic `system.py:156` (removed, see below) |
| preprocessing | `pyannote_auth_token` | `""` | **secret** — should not live in app DB even masked | `services/vad.py:36,82`; cache-diff logic `system.py:157` (removed, see below) |

`app/core/settings.py` currently defines none of these names — new
`Settings` fields are added from scratch (pydantic-settings, same pattern as
existing fields like `admin_password`), each with the current default baked
in as the fallback so behavior is unchanged for anyone not setting the env
var.

The old-vs-new diff in `system.py`'s PUT handler that triggers
`clear_pyannote_cache()` when `pyannote_vad_model`/`pyannote_auth_token`
change is deleted — these become env vars, so changing them requires a
process restart, which already resets any in-memory cache.

### Delete entirely (dead code)

`extra_warmup_stt_engines`, `extra_warmup_tts_engines` (both in `engines`) —
grep confirms **zero read call sites**; only reference is a stale comment at
`app/main.py:46`. Remove the fields, the comment, and their UI rows.

### Delete + collapse the STT/TTS engine override tier (config + default only, no query param)

`conversation_stt_engine`/`conversation_tts_engine` (a system-wide
conversation override) **and** the per-request `stt_engine`/`tts_engine`
query-param override are both removed. The resolution chain collapses to
just two tiers: **profile config, then system default** —
`profile.stt.engine > default_stt_engine` (same pattern for TTS: a matched
`tts_profile` entry, then `default_tts_engine`). No query-param tier, no
system-wide conversation-tuning override.

This is confirmed *not* dead-flexibility removal for the query-param tier —
it has two real, currently-working consumers that this change intentionally
breaks:

1. **Chat/Livehost engine-select dropdown**: `app/static/js/conversation.js`
   (`<select>` at line 297, sends `&stt_engine=...` on WS connect at line
   322) and `app/static/js/livehost.js` (line 249 / line 275, same pattern).
   These dropdowns become non-functional once the backend stops reading
   `stt_engine` from the query string — **remove the dropdown control from
   both pages** rather than leave a UI element that silently does nothing.
   (TTS has no equivalent dropdown — the UI only ever sent `tts_profile`,
   never `tts_engine`, so no TTS-side UI change needed.)
2. **Documented raw device-connect protocol** (devices connecting without a
   Lugo profile): `docs/api.md:124-144`, `docs/device-integration.md:24-72`,
   `rpi-assistant/integration.md:24-45`, `esp32-assistant/README.md:147` all
   document `?stt_engine=&tts_engine=` as how a raw-connecting device picks
   its engine. Update all four to remove this — raw-connecting devices now
   always get `default_stt_engine`/`default_tts_engine` (or a profile match,
   if they do send a profile), with no per-connection override. This is an
   accepted, intentional behavior change to the device protocol, not an
   oversight.

Code call sites to update:

- `app/services/stt/profile.py:10-40` (`resolve_stt`) — drop the `q_engine`
  parameter entirely (its only use was this tier); resolution becomes
  `stt_cfg.engine or default_stt_engine`. `q_language`/`q_model` are
  untouched — this change is scoped to engine selection only, not
  language/model overrides, which are a separate, unaffected concern.
- `app/api/routes/conversation.py:221-223`, `app/api/routes/livehost.py:105-107`
  — stop passing `q.get("stt_engine")` into `resolve_stt`.
- `app/api/routes/conversation.py:239-243`, `app/api/routes/livehost.py:123-127`
  — drop both `q.get("tts_engine") or` and `conv_cfg.conversation_tts_engine or`,
  leaving just `default_tts_engine` as the tail-end fallback after any
  `tts_profile` match.
- `app/api/routes/lugo.py:51,61` — already never passed a query param
  (comment: "No query params on the Lugo wire") and its TTS line already had
  no query tier; only the `conversation_tts_engine or` removal applies here
  (no additional change beyond the field's removal below).
- `app/services/system_config.py:255,265` (`warmup_stt_engines()` /
  `warmup_tts_engines()`) — currently warm up
  `conversation_stt_engine`/`conversation_tts_engine` as the primary engine;
  switch to `config.engines.default_stt_engine`/`default_tts_engine`.

**Explicitly out of scope for this removal**: `/v1/stt/transcribe` and
`/v1/stt/stream`'s own `engine` request parameter (`stt.py:54,165`) are
unaffected — those are standalone batch/stream API calls where the caller
is expected to name an engine per request; they don't go through
`resolve_stt` and aren't part of the profile/config/default chain being
simplified here.

Fields removed from `ConversationTuningConfig`: `conversation_stt_engine`,
`conversation_tts_engine`. `conversation_fast_stt_engine` is **not**
affected — it's a genuinely separate, independent low-latency fast-path
tier (see its description below), not a duplicate of the default-engine
concept.

Tests to update (construct URLs with `?stt_engine=&tts_engine=` or assert
the old 4-tier chain — need the query-param cases removed/rewritten):
`tests/unit/test_stt_profile.py:43,73`,
`tests/unit/test_conversation_tts_profile.py:96-160`,
`tests/unit/test_livehost_tts_profile.py:96-136`,
`tests/integration/test_conversation_ws.py:82-139`,
`tests/integration/test_livehost_ws_voice.py:66-176`.

**Behavior note**: today `default_stt_engine` (`"vosk"`, light, used by
batch endpoints) and `conversation_stt_engine` (`"whisper"`, more accurate,
used by live conversation) intentionally differ, and either could
previously be overridden per-connection via query param. After this change,
batch transcription and live conversation share the single
`default_stt_engine` value, and no per-connection override exists at all —
the only remaining lever is a profile-level override
(`profile.stt.engine`/a matched `tts_profile`). Flagging this so the
deploy-time default choice for `default_stt_engine`/`default_tts_engine` is
a conscious decision, not a silent regression.

### Merge into Engine Defaults, drop the "STT (Shared Settings)" group

`stt_segment_long_enabled`, `stt_segment_min_seconds`,
`stt_segment_concurrency` (currently in `stt_local`) are read exclusively by
the batch endpoint `app/api/routes/stt.py:72,74,81` — never by the
conversation flow. They're not "shared" (the group's current name is
misleading) and don't warrant a standalone top-level accordion once the four
path/URL fields move out of `stt_local`. They move into `EngineDefaults` as
a second sub-block, "Long-audio segmentation (batch STT)". `SttLocalConfig`
as a class is deleted; `SystemConfig.stt_local` is removed.

**Net result: the page goes from 4 top-level accordion groups to 3**
(Engine Defaults, Conversation Tuning, Preprocessing).

### Stays in admin UI (live-tunable)

- **Engine Defaults** (`engines`, `EngineDefaults` model, 6 real fields
  across 2 display sub-blocks). Sub-block 1, "Engine selection":
  `default_stt_engine`, `default_tts_engine`, `default_tts_engine_voice`,
  plus the synthetic `DEFAULT_LLM` select (not a real model field — unchanged
  behavior, still auto-saves on change via `/v1/model_registry` PATCH, not
  part of the group Save). Sub-block 2, "Long-audio segmentation (batch
  STT)": `stt_segment_long_enabled`, `stt_segment_min_seconds`,
  `stt_segment_concurrency` (moved in from the deleted `stt_local` group,
  see below).
- **Conversation Tuning** (`conversation`, 19 fields after removing
  `conversation_stt_engine`/`conversation_tts_engine` above — this group's
  entire purpose is live tuning of conversation UX/latency), organized into
  4 display sub-blocks (via field metadata, JSON shape unchanged):

  | Sub-block | Fields |
  |---|---|
  | Timing & VAD | `conversation_silence_ms`, `conversation_min_silence_ms`, `conversation_adaptive_full_ms`, `conversation_min_speech_ms`, `conversation_rms_threshold`, `conversation_preroll_ms`, `conversation_barge_in_grace_ms`, `conversation_max_utterance_ms` |
  | STT | `conversation_fast_stt_engine`, `conversation_fast_stt_max_ms`, `conversation_streaming_stt`, `conversation_streaming_chunk_ms` |
  | TTS & Audio | `conversation_tts_lookahead`, `conversation_opus_pace`, `conversation_opus_prebuffer_frames` |
  | Language & Prompt | `conversation_language`, `conversation_goodbye_text`, `conversation_system_prompt` (→ `<textarea>`), `llm_timeout_seconds` |

- **Preprocessing** (`preprocessing`, now 4 fields): `stt_vad_enabled`,
  `stt_vad_backend`, `stt_noise_reduce_enabled`, `stt_noise_reduce_amount`.

## Migration note (deploy-time, not code)

Before shipping this change to the running Coolify instance: read the
current live `SystemConfig` (via `GET /v1/system/config`) and, for any of
the 9 fields moving to env, set the equivalent env var in Coolify if the
live value differs from the hardcoded default (e.g. if `vosk_model_base_url`
was ever customized). Otherwise the value silently reverts to the new
code-level default on deploy. This is a manual step to perform at deploy
time, not something the migration code needs to automate — legacy JSON
import (`_import_legacy`) is unaffected since it only touches `base_context`.

## Field metadata (labels, descriptions, sub-grouping, units)

Sub-grouping and human-readable text are added **without changing the JSON
shape** of `SystemConfig`, so none of the ~30 remaining call sites are
touched:

- Every field in `EngineDefaults`, `ConversationTuningConfig`,
  `PreprocessingConfig` gets a `Field(title=..., description=...,
  json_schema_extra={"subgroup": ..., "unit": ...})` annotation in
  `system_config.py`. `title`/`description` are English (matches the rest
  of the admin UI). Fields may also be **reordered within the class body**
  to match display order — safe, since no consumer depends on Python
  attribute declaration order (confirmed in blast-radius research).
- A new endpoint `GET /v1/system/config/meta` (`app/api/routes/system.py`)
  introspects `SystemConfig`'s nested model fields (via
  `<Model>.model_fields`) and returns:
  ```json
  {
    "engines": {
      "default_stt_engine": {
        "label": "Default STT engine",
        "description": "Used for standalone transcription (/v1/stt/transcribe, /v1/stt/stream) and for live voice conversations (unless overridden per-profile).",
        "subgroup": "Engine selection",
        "unit": null
      },
      "stt_segment_long_enabled": {
        "label": "Enable long-audio segmentation",
        "description": "Split long recordings into chunks and transcribe them in parallel (batch endpoint only).",
        "subgroup": "Long-audio segmentation (batch STT)",
        "unit": null
      }
    },
    "conversation": { "...": "..." },
    "preprocessing": { "...": "..." }
  }
  ```
  Built from field introspection (not hand-maintained as a parallel
  structure) so adding/removing a `SystemConfig` field can't silently drift
  out of sync with its metadata.

### Descriptions that resolve field-relationship confusion

These specific descriptions are called out because the fields they document
have non-obvious override/fallback relationships (confirmed via code trace,
not assumed):

- `engines.default_stt_engine`: *"Used for standalone transcription
  (/v1/stt/transcribe, /v1/stt/stream) and for live voice conversations
  (unless overridden per-profile)."*
- `engines.default_tts_engine`: same pattern, mirrored description text for
  TTS.
- `conversation.conversation_fast_stt_engine`: *"Optional low-latency engine
  used only for short utterances (≤ Fast STT max ms). Independent of Default
  STT engine above — no fallback relationship, just an opt-in fast path."*

## Frontend changes (`app/static/js/system-config.js`, `styles.css`)

- `loadSystemConfigGroups()` fetches `/v1/system/config/meta` once alongside
  the existing `/v1/system/config` data fetch, caches it.
- `GROUPS` shrinks to 3 entries (`engines`, `conversation`, `preprocessing`);
  `stt_local` entry removed.
- `renderGroupFields()` rewritten to: look up each field's metadata (label,
  description, subgroup, unit) instead of using the raw key as the label;
  cluster fields by `subgroup` into `<div class="field-subgroup">` blocks
  with a small heading, inside the existing `<details>`; render
  `conversation_system_prompt` as `<textarea rows="4">` (detected via a
  `multiline: true` metadata flag on that field specifically, not a
  name-based heuristic); append the unit (e.g. "ms", "s") to the label when
  present; render `description` as a `.hint`-styled line under the
  label/input.
- **Save becomes per-group**: each `<details>` gets its own Save
  button/status line instead of one shared `#sys-config-groups-save` at the
  bottom. `saveSystemConfigGroups(groupKey)` now takes a group key, reads
  only that group's inputs by the existing `sys-{groupKey}-{field}` id
  convention, and PUTs `{ [groupKey]: {...} }` — the backend's existing
  deep-merge means this already worked for partial payloads; only the JS
  needs to stop collecting all 3 groups into one request.
- CSS: style `<details>/<summary>` to match the app's existing "technical
  readout" design system (`--accent`, `--card`, `--line` variables already
  in `styles.css`) — border, accent-colored summary arrow/hover state,
  consistent spacing. `.field-subgroup` uses the existing `.row.tight`
  (2-column grid) pattern already used by Model Registry's "Add Entry" form,
  with a small subgroup heading (reusing `.status-group h3.sub` styling from
  the sibling Status subtab for visual consistency). `.hint` (already
  defined) is reused for per-field description text.

## Testing

- Backend: rewrite the 5 test files listed above to drop query-param-engine
  cases and assert the new 2-tier chain (`profile.stt.engine >
  default_stt_engine`, matched `tts_profile` > `default_tts_engine`); unit
  test that `warmup_stt_engines()`/`warmup_tts_engines()` now warm
  `default_stt_engine`/`default_tts_engine`. Unit tests for
  `GET /v1/system/config/meta` (shape, all 3
  remaining groups present, no leaked reference to removed fields); test
  that `SystemConfig` no longer has `stt_local`; test that PUT to
  `/v1/system/config` with only `{"engines": {...}}` doesn't touch
  `conversation`/`preprocessing` (existing deep-merge behavior, regression
  guard). Unit tests for each of the 9 relocated fields now reading from
  `settings.<field>` at their call sites, with env var override tested via
  monkeypatch.
- Frontend: manual pass in browser — load Settings subtab, confirm 3 groups
  render with sub-headings, textarea for system prompt, per-group Save
  works independently (change one group, Save, confirm other groups'
  unsaved edits aren't sent), descriptions/units visible. Separately, on the
  Chat and Livehost pages: confirm the STT engine-select dropdown is gone
  and a voice session still connects/works using whatever
  `default_stt_engine`/profile override applies.
- Full backend test suite run before merge (per project convention — main
  auto-deploys to prod, tests must pass first).

## Out of scope / follow-ups

- Whether `pyannote_auth_token` should use a `SecretStr`-style env
  convention beyond what `app/core/settings.py` already does for other
  secrets — follow existing convention, no new pattern introduced here.
- Any further reduction of Conversation Tuning's remaining 19 fields — this
  spec keeps the rest of that group as-is; it's explicitly the "live tuning"
  surface.
