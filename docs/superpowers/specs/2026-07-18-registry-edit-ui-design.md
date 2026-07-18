# Model Registry admin UI — Edit expand + config form

Date: 2026-07-18
Status: approved, ready for implementation plan

## Problem

Every registry row shows API Key, Base URL, and Config as always-visible editable
cells (`model-registry.js:56-85`). The table is wide and noisy: a password box, a
URL box, and a raw-JSON textarea sit on every row whether or not that row uses
them. Config is raw JSON only — an admin editing `whisper_local`'s `beam_size` has
to hand-edit `{"default_model":"large-v3-turbo","vad_filter":true,"beam_size":1,...}`
without a mistake.

## Goal

Collapse the three editable fields behind a per-row **Edit** button. The main row
stays compact (Kind, Engine/Model, Label, Stage, actions). Clicking Edit expands a
detail row spanning the table width, holding API Key, Base URL, and Config. Config
gains a **Form** mode alongside the existing **Raw JSON**, with the form's fields
driven by a backend schema so it never drifts from the code.

## Architecture

Three units, each independently testable:

1. **Backend schema endpoint** — `GET /v1/model_registry/config_schema`, the single
   source of truth for a form's fields.
2. **`data-table.js` detail-row support** — a generic `rowDetail(row)` hook,
   reusable beyond this table.
3. **`model-registry.js` edit UI** — the Edit toggle, the detail-row content, and
   the Form/Raw config editor with type-correct save.

## Component 1: schema endpoint

`GET /v1/model_registry/config_schema?kind=<kind>&engine=<engine>` → admin-gated
(same `_ADMIN_PREFIXES` as the rest of `/v1/model_registry`).

Returns:

```json
{"fields": [
  {"key": "default_model", "type": "str",  "default": "large-v3-turbo"},
  {"key": "vad_filter",    "type": "bool", "default": true},
  {"key": "beam_size",     "type": "int",  "default": 1},
  {"key": "condition_on_previous_text", "type": "bool", "default": false},
  {"key": "initial_prompt", "type": "str", "default": ""}
]}
```

**Field source (no duplication — the same values `resolve.py` already uses):**

- STT local (`whisper_local`, `whisper_mlx`, `qwen3_asr`, `vosk`) → the engine's
  entry in `STT_ENGINE_CONFIG_DEFAULTS` (`resolve.py:78`).
- Remote STT/TTS (`openai_stt`, `openai_tts`, `whisper_service`, `eventlab`) →
  `{"timeout_seconds": <float, default 60.0>}` — the one key those providers read
  from `config`.
- `omnivoice` → the fields of `OmnivoiceConfig` (`system_config.py:48`), which is
  what the registry stores for it.
- Anything else (`llm`, other TTS) → `{"fields": []}`.

**Type** is inferred from the default value's Python type — `bool → "bool"`,
`int → "int"`, `float → "float"`, `str → "str"`. `bool` is checked before `int`
(bool is an int subclass in Python), the same ordering the env layer already uses.

The endpoint reads schemas, never an entry — it describes a `(kind, engine)`, not a
stored row. It does not touch the database.

## Component 2: `data-table.js` detail rows

Add an optional `rowDetail(row) -> htmlString | null` to `renderDataTable`'s config.
When present, each main `<tr>` is followed by a hidden `<tr class="dt-detail">` with
a single `<td colspan="<n>">` containing `rowDetail(row)`'s HTML. A helper exposes
toggling a row's detail open/closed by row key (so `model-registry.js` can wire its
Edit button). `colspan` equals the column count including the checkbox cell.

Rows whose `rowDetail` returns null get no detail row (keeps the hook opt-in for
other tables). Default behavior with no `rowDetail` is unchanged — this is additive.

## Component 3: `model-registry.js` edit UI

**Main row** loses the API Key, Base URL, and Config columns. It keeps Kind,
Engine/Model, Label, Stage, and an actions cell with two buttons: **Edit** (toggles
the detail row) and the existing Enable/Disable.

**Detail row** (`rowDetail`) contains:

- **API Key** — password input + a `not set`/`sk-…363` hint. Blank submit keeps the
  existing key (the app-wide "blank means keep" convention, `model-registry.js:117`).
- **Base URL** — text input, prefilled from `entry.base_url`.
- **Config** — a `[Form] [Raw JSON]` toggle over the editor.

**Config Form mode:** on first open, fetch `config_schema` for the row's
`(kind, engine)`. Render one input per field by type: `bool → checkbox`,
`int`/`float → number`, `str → text`. Prefill from `entry.config`; the schema
default is the placeholder. If the schema returns no fields, show "No preset fields
for this engine — use Raw JSON" and leave only Raw usable.

**Config Raw mode:** the existing JSON textarea, seeded from `entry.config`.

**Two-way sync:** switching Form→Raw serializes the form to JSON; Raw→Form parses
the JSON back into the fields. If the Raw JSON is invalid, the Form toggle is
disabled and an inline error shows until it parses — Raw stays editable.

**Save:** one Save button per detail row. The **currently visible** config mode is
the source — Form gathers its inputs, Raw parses its textarea. Persist via the
existing `patchEntry(id, {api_key?, base_url?, config})`; the backend PATCH is
unchanged.

**Type coercion (the sharp edge):** a number input yields the string `"1"`, a
checkbox a bool. Before building the config object, Form mode coerces each value to
its schema `type` — `int`/`float` via `Number(...)` (rejecting `NaN` with an inline
error), `bool` from the checkbox's `checked`, `str` as-is. Sending `beam_size:"1"`
instead of `1` would reach `whisper_local` and could fail at transcribe time, so
this coercion is mandatory, not cosmetic.

## Error handling

- Schema fetch fails (network/500) → Config falls back to Raw-only with an inline
  note; the row is still editable.
- Invalid Raw JSON on save → inline error, no PATCH sent (current behavior,
  `model-registry.js:131`).
- Form value fails coercion (e.g. `beam_size` = "abc") → inline error on that field,
  no PATCH sent.
- PATCH rejected by the backend's test-before-save → surface the backend message
  (current `patchEntry` behavior).

## Testing

- **Backend:** the endpoint returns the right fields+types for `whisper_local` (rich,
  mixed types), `openai_tts` (only `timeout_seconds`, float), `omnivoice` (the
  OmnivoiceConfig fields), and an unknown engine / `llm` (empty `fields`). Assert
  `bool` is reported as `"bool"` not `"int"`.
- **Frontend:** this repo has no JS test runner, so verify by driving the real UI —
  open Edit on a `whisper_local` row, toggle `vad_filter`, change `beam_size`, Save,
  and confirm the PATCH body carries `beam_size` as a number and `vad_filter` as a
  bool (not strings). Confirm Form↔Raw round-trips a config without loss, and that
  an unknown-engine row shows Raw-only.

## Risks

- **Type coercion** is the highest-risk area (see above) and the frontend
  verification must target it specifically.
- **OmnivoiceConfig has 14 fields** — a long form, but acceptable in an expanded
  detail row. Not paginated or grouped in v1.
- The detail-row change to `data-table.js` is shared with any other table using it;
  the additive/opt-in design keeps existing tables unchanged, but the implementer
  must confirm no current caller breaks.

## Out of scope

- Validating config values against the backend beyond type coercion (the backend's
  own test-before-save already gates a bad config).
- A schema for LLM config (LLM stays free-form — Raw JSON only).
- Grouping/collapsing the 14 OmniVoice fields.
- Changing the Add-entry form (`registry-add-*`) — this spec is the row editor only.
