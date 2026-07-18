# Web Client Profile Selection & Management — Design

**Date:** 2026-07-18
**Status:** Approved (design), pending implementation plan
**Repos affected:** `lugo-web-client` (submodule) only. No backend changes.

## Problem

The web client (`lugo-web-client`) never sends a profile when opening the
conversation WebSocket. `audio/conversation.ts` builds a static `PARAMS` with no
`profile`, and the UI has no way to select one. So every web talk session falls
through to the server-wide STT/TTS/LLM defaults instead of a profile's config.

This surfaced as a crash: the server-default STT engine resolved to
`whisper` → `whisper_local`, whose registry sentinel carried an invalid
`default_model` (`phowhisper-medium`, not a faster-whisper size), raising
`ValueError: Invalid model size`. That specific data bug was fixed separately
(local `data/app.db`: default STT engine → `qwen3_asr`; whisper_local
`default_model` → `large-v3-turbo`). This spec addresses the underlying gap:
**the web client should let users pick and manage profiles**, so a session runs
under a real profile rather than server defaults.

## Backend context (already exists, unchanged)

- `Profile.owner_id`: `None` = shared template (visible to all; only admin
  writes); non-null = private to that user (only they see/write).
- `GET /v1/profiles` → `{success, data: {<name>: <masked profile>}}` — returns
  **shared templates + the caller's own profiles**. `llm.api_key` masked to
  `"***"`.
- `GET /v1/profiles/{name}` — one masked profile.
- `POST /v1/profiles` — create (409 if name exists; non-admin caller →
  `owner_id = caller`).
- `PUT /v1/profiles/{name}` — **full-replace** upsert. Ownership-gated for an
  existing row. Blank `llm.api_key` in payload preserves the stored key.
- `DELETE /v1/profiles/{name}` — delete own row (or template if admin).
- `POST /v1/profiles/{name}/clone` — clone any visible profile into a new
  `owner_id = caller` copy.
- `GET /v1/profiles/llm-options` → `{data: [{id, engine, model_id, label}]}`.
- `GET /v1/tts/profiles` → `{data: {<name>: <tts profile>}}` (same visibility
  pattern) — for the TTS dropdown.
- Server validates on write (`_validate_profile_models`): e.g. `stt.model`
  requires `stt.engine` or a resolvable `stt.profile` preset; model-allowed
  checks. Errors come back as JSON `{error|detail: <text>}`.

**Consequence for the client:** since the list returns only templates + my own,
`owner_id === null` ⇒ shared (Clone only); `owner_id !== null` ⇒ mine
(Edit/Delete). The client does **not** need the current user id.

**Full-replace gotcha:** `PUT` rebuilds the whole `Profile` from the payload.
The editor is a *full* form (every field present), so every field is sent back
and nothing is silently reset. `api_key` is the one exception: it loads back as
`"***"` (masked) and MUST NOT be sent as-is — the field loads blank; blank means
"keep existing".

## Architecture

Routing is state-based (no router library): `App.tsx` maps a `Screen` union to a
component; `Nav.tsx` lists the tabs. We extend both with a `profiles` screen.

Two concerns, built together:

1. **Talk picker** — choose which profile a conversation runs under.
2. **Profiles management screen** — full CRUD over the caller's own profiles,
   plus clone-from-template.

## Components

### New files (`lugo-web-client/src/`)

- **`api/profiles.ts`** — TypeScript types mirroring the backend `Profile`
  (`LlmConfig`, `SttConfig`, `TtsConfig`, `McpServer`, `MemoryConfig`,
  `SessionConfig`) and functions:
  - `listProfiles(): Promise<Profile[]>` — `apiFetch('/v1/profiles')`, dict→array.
  - `getProfile(name): Promise<Profile>`
  - `createProfile(p): Promise<Profile>` — `POST`
  - `updateProfile(name, p): Promise<Profile>` — `PUT`
  - `deleteProfile(name): Promise<void>` — `DELETE`
  - `cloneProfile(name, newName): Promise<Profile>` — `POST /clone`
  - `listLlmOptions(): Promise<LlmOption[]>`
  - Error surfacing: an `errorFrom(resp)` that reads `{error|detail}` and keeps
    the server's text (pattern from `api/devices.ts`); map 409 → "name taken".
- **`api/tts.ts`** — `listTtsProfiles(): Promise<{name; nickname?}[]>` for the
  TTS dropdown.
- **`screens/Profiles.tsx`** — list split into **Shared** (owner_id null → Clone)
  and **Mine** (Edit / Delete / Clone); a **New** button; Delete confirmed via
  the existing `ui/ConfirmModal`.
- **`screens/ProfileEditor.tsx`** (+ `screens/Profiles.css`) — the full form.

### Modified files

- **`audio/conversation.ts`** — `Conversation` accepts `profile?: string`;
  replace the static `PARAMS` const with `buildParams(profile?)` that keeps
  `audio_out/output/sample_rate/output_sample_rate` and adds `profile` only when
  set. Token still travels via subprotocol; profile via query string.
- **`screens/Talk.tsx`** — on mount (idle) `listProfiles()`, render a `<select>`
  in `talk__bar` labelled "Assistant". Selection persists in
  `localStorage['lugo.talkProfile']`; on load use the saved value if still valid,
  else **auto-select the first profile**. `<select>` disabled while a call is
  live. `start()` passes the selected name into `new Conversation({…, profile})`.
  If the list is empty or the fetch fails, hide the dropdown and let Start run
  with no profile (server default) so talk is never blocked.
- **`components/Nav.tsx`** — add `{ id: 'profiles', label: 'Profiles' }` to the
  `Screen` union and `ITEMS`.
- **`App.tsx`** — add `profiles: Profiles` to `SCREENS`.

## Profile editor form (full)

Loaded via `getProfile(name)` for edit; blank template for create. Fieldsets:

- **Basic:** `name` (readonly on edit — it is the key/URL; renaming = clone),
  `nickname`, `voice_optimized`.
- **LLM:** `engine` + `model` (dropdown from `listLlmOptions`, plus free-text
  entry), `base_url`, `api_key` (password; loads blank; blank = keep existing;
  never send `"***"`).
- **System prompt:** textarea.
- **STT:** `profile` (select `'' | vi | en | multi | en_vi`), `engine`,
  `language`, `model` (text).
- **TTS:** `profile_name` (select from `listTtsProfiles`, plus blank).
- **MCP servers:** dynamic rows `{name, url, enabled, headers}` with add/remove;
  `headers` edited as a JSON textarea (parsed on save; parse error blocks save
  with an inline message).
- **Memory:** `enabled`, `mode` (`all | semantic`), `top_k`, `extractor_model`,
  `embed_model`, `compaction_threshold`, `max_facts`, `dedup_threshold`.
- **Session:** `idle_timeout_s`.

**Save:** assemble a full `Profile` object → `createProfile` (new) or
`updateProfile` (existing). After success, close the editor and refetch the list.

## Data flow

- Talk: `listProfiles()` → `<select>` → `localStorage` → `Conversation.connect`
  builds `…/stream?…&profile=<name>` → backend `resolve_stt`/TTS use the profile.
- Management: `Profiles.tsx` ↔ `api/profiles.ts` ↔ backend CRUD. Every mutation
  is followed by a refetch (no optimistic updates).

## Error handling

- API helpers surface the server's error text verbatim (validation messages from
  `_validate_profile_models`, model-not-allowed, etc.).
- 409 on duplicate name → friendly "that name is taken".
- MCP `headers` JSON parse error → inline, blocks save.
- `onAuthLost` (existing) still routes to Login on refresh failure.

## Testing (Vitest, matching the existing screen-test suite)

- `api/profiles.ts` — each call parses its response; dict→array; `owner_id`
  preserved; error text surfaced; 409 mapped.
- `audio/conversation.ts` — WS URL includes `?profile=<name>` when set and omits
  it when not; existing params preserved.
- `screens/Talk.tsx` — renders dropdown from fetched profiles; auto-selects first
  when nothing saved; restores a saved selection; persists on change; disabled
  while live; passes the selected profile to `Conversation`; empty/failed list
  still allows Start.
- `screens/Profiles.tsx` — renders Shared vs Mine grouping from `owner_id`;
  actions call the right API; Delete goes through `ConfirmModal`.
- `screens/ProfileEditor.tsx` — loads all fields; `api_key` blanked on load and
  never sent as `"***"`; create vs update path; MCP row add/remove; JSON header
  parse error blocks save; server validation error shown.

## Out of scope (YAGNI)

- No backend changes (CRUD already sufficient).
- No optimistic UI.
- No admin-only editing of shared templates from the web client (backend already
  forbids it; the UI offers Clone instead).
- No per-user "default profile" concept server-side (profile is chosen per
  session via the picker).

## Build order

Single implementation pass covering both concerns. Suggested internal ordering
for the plan: (1) `api/profiles.ts` + `api/tts.ts`, (2) `conversation.ts` +
Talk picker, (3) Nav/App wiring + `Profiles.tsx`, (4) `ProfileEditor.tsx`. Each
with tests first (TDD).
