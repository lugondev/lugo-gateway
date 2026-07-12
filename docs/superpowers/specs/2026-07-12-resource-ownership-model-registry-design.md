# Resource ownership & model registry (Follow-ups 1+2 from the identity/auth spec)

## Problem

The identity/auth foundation (previous spec) added real user accounts, roles, and
device pairing, but every existing config resource — `Profile`, `TtsProfile`,
`McpServer` — is still process-global: any logged-in user can read and mutate all of
them, including MCP server definitions that hold API keys/auth headers. Chat history
(`ChatSession`/`ChatMessage`) and long-term memory (`MemoryItem`/`MemoryProfileDoc`)
are equally ownerless — any user who knows or lists a `session_id` can read/delete
anyone's conversation. Combined with the self-signup flow the identity/auth spec
intentionally enabled, this means any stranger who registers an account today can
read every other user's secrets and conversation history.

Separately, the user has asked for a way to gate which STT/TTS/LLM engine-model
choices are usable at all: an `enabled` switch per model, and a `testing` stage
selectable only by users with the `can_use_testing` flag (added, but unused, on the
`User` model in the prior spec).

## Scope

**In scope:**
1. `owner_id` on `Profile`, `TtsProfile`, `McpServer` (stored as a plain field in
   their existing JSON-blob rows — no DB schema change for these three). Admin-created
   rows are templates (`owner_id = None`); user-created rows are private
   (`owner_id = <user_id>`). List/get/update/delete scoped to "visible to me"
   (my own templates-view = all templates + only my own private rows — never another
   user's private rows).
2. A `POST .../{name}/clone` action on all three resource types: copies a visible
   source row (a template, or already my own) into a new row owned by the caller.
3. `user_id` column on `ChatSession` and `MemoryItem`/`MemoryProfileDoc` — these ARE
   real SQL tables, so this needs a lightweight in-process migration (no Alembic in
   this codebase) rather than relying on `Base.metadata.create_all`, which only
   creates missing tables, never alters existing ones.
4. Session/memory ownership rule: a session/memory item takes the `user_id` of the
   *profile's* `owner_id` if the profile in use is user-owned, else `None` (shared,
   matching today's behavior) — reusing the ownership signal already on `Profile`
   rather than inventing a second one.
5. `GET/DELETE /v1/sessions*`: non-admin sees/acts on only sessions where
   `user_id` matches them; admin retains full visibility (unchanged from today) —
   this mirrors the Devices page's admin-oversight pattern, not the MCP-secrets
   privacy pattern, since chat history isn't a credential.
6. A new `model_registry_entries` table: `(kind: stt|tts|llm, engine, model_id, label,
   enabled, stage: stable|testing)`, unique on `(kind, engine, model_id)`. Auto-seeded
   from STT's existing `STT_MODEL_REGISTRIES`/`whisper_manager`/`Qwen3AsrModelRegistry`
   and from installed TTS engines (`tts_service.providers`); LLM entries are always
   admin-added manually (no discoverable list exists for arbitrary OpenAI-compatible
   endpoints).
7. Validation gate: when a `Profile`/`TtsProfile` is created/updated and its chosen
   `(kind, engine, model_id)` matches a registry entry, that entry must be `enabled`,
   and `stage="testing"` requires the acting user's `can_use_testing`. A choice that
   matches *no* registry entry (a fully custom self-hosted LLM, for example) is
   **not** blocked — the registry only exercises authority over entries an admin
   explicitly catalogued, preserving the existing bring-your-own-endpoint flexibility.
8. `LlmConfig.engine: str = ""` — new field (parallels `SttConfig.engine`), needed so
   an LLM choice can be matched against the registry by `(engine, model)`; blank means
   "custom, not gated."
9. Manually adding a registry entry (`POST /v1/model_registry`, admin-only) runs a
   real test call for that `(kind, engine, model_id)` *before* the row is persisted —
   reusing existing provider/responder code (`OpenAICompatResponder.reply`,
   `tts_service.get_provider(...).synthesize(...)`,
   `stt_service.get_provider(...).transcribe_bytes(...)`) — and rejects the create
   (400, with the underlying error) if the test fails. Auto-seeded entries (from
   already-installed engines) are not re-tested at seed time.
10. Admin UI: a new "Model Registry" page (list all entries, toggle
    enabled/stage, add a new entry with its blocking test-call). Profile/TtsProfile
    edit forms are **not** changed to a filtered dropdown — they keep today's free-text
    engine/model inputs; the registry only rejects an invalid choice server-side on
    save, exactly like the existing STT `registry.validate()` check.

**Out of scope, with rationale:**
- Admin cross-user visibility into other users' private `Profile`/`TtsProfile`/
  `McpServer` rows. These can hold secrets (MCP headers) or personal customization;
  default is privacy, not oversight — the opposite tradeoff from Devices/Sessions,
  made deliberately per-resource, not uniformly.
- A "reset clone to template" or any live link back to the source template after
  cloning. Clones are fully independent copies from the moment they're created
  (decided during design — simplicity over template-tracking complexity).
- Extending the model registry's blocking-test-on-add requirement to the STT/TTS
  auto-seed path. Auto-seeded entries come from engines already installed and running
  on this server; re-testing every known model at every seed pass is redundant cost,
  not a correctness gap.
- A filtered, registry-aware dropdown in the Profile/TtsProfile edit UI. Free-text
  input + server-side rejection matches the existing STT validation UX and avoids a
  much larger frontend change (populating live-filtered dropdowns keyed by role and
  `can_use_testing`).
- Per-message-level memory sharing rules beyond the profile-ownership signal reuse
  (item 4). If a shared template profile's memory should ever be split per-user, that
  is new scope, not implied by anything requested here.

## Component 1 — `owner_id` on `Profile`/`TtsProfile`/`McpServer`

Add `owner_id: str | None = None` to all three Pydantic models
(`apps/api_gateway/app/services/profiles/models.py`'s `Profile`,
`apps/api_gateway/app/services/tts/profile_models.py`'s `TtsProfile`,
`apps/api_gateway/app/services/mcp/models.py`'s `McpServer`). Because
`SqliteBackedStore` persists these as an opaque JSON blob keyed by `name`
(`apps/api_gateway/app/services/db/config_store.py`), adding a field to the Pydantic
model is the entire "migration" — no new column, no `ALTER TABLE`, existing rows
deserialize `owner_id` as `None` (already-existing templates stay templates).

Each of the three route files (`api/routes/profiles.py`, `api/routes/tts_profiles.py`,
`api/routes/mcp.py`) gets the same shape of change:
- `list_*(request: Request)`: `store.list()` then filter to
  `{k: v for k, v in all.items() if v.owner_id is None or v.owner_id == request.session["user_id"]}`.
- `create_*(payload, request: Request)`: `owner_id = None if request.session["role"] == "admin" else request.session["user_id"]`,
  set on the constructed model before `store.upsert(...)`.
- `get/update/delete_*(name, request: Request)`: look up the row; if it doesn't exist
  *or* isn't visible to the caller (not a template, not theirs), respond 404 (not 403 —
  don't reveal existence of another user's private row).
- New `POST /v1/profiles/{name}/clone {new_name}` (and the TtsProfile/McpServer
  equivalents): look up `name` with the same visibility rule as `get`; 404 if not
  visible; 409 if `new_name` collides with any name visible to the caller; otherwise
  construct a copy with `name=new_name`, `owner_id=<calling user>`, and every other
  field copied verbatim, then `store.upsert(...)`.

## Component 2 — `user_id` on `ChatSession`/`MemoryItem`/`MemoryProfileDoc`

Unlike Component 1, these are real SQLAlchemy tables
(`apps/api_gateway/app/services/db/models.py`) already populated in production/dev
databases — `Base.metadata.create_all()` only creates *missing* tables, so a new
column on an *existing* table needs an explicit, idempotent migration step, since this
codebase has no Alembic.

Add a small helper to `apps/api_gateway/app/services/db/engine.py`, run once inside
`init_db()` right after `create_all`:

```python
async def _ensure_column(conn, table: str, column: str, ddl_type: str) -> None:
    result = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
    existing = {row[1] for row in result.fetchall()}  # row[1] = column name
    if column not in existing:
        await conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
```

Called as `await _ensure_column(conn, "sessions", "user_id", "VARCHAR(36)")` and the
same for `"memories"` and `"memory_profile_docs"`, inside the same `engine.begin()`
block `create_all` already runs in. `ChatSession.user_id`/`MemoryItem.user_id`/
`MemoryProfileDoc.user_id` are added as `Mapped[str | None]` (nullable — pre-existing
rows get `NULL`, meaning "created before this feature, unowned," which only an admin
can see, matching how a new column with no backfill should behave).

**Where `user_id` gets set:** `ConversationSession` (used by all four WS routes) and
the REST `/v1/conversation/chat` handler already resolve `identity.user_id` (from
`resolve_ws_identity`) or `request.session["user_id"]` respectively. Thread that value
into `session_store.create(...)` (new optional `user_id` param,
`apps/api_gateway/app/services/history/store.py`) and into
`memory_extractor.extract_and_upsert(...)`/`memory_store.add(...)` — but per the
ownership rule (Scope item 4), the value actually stored is **not** always the
connecting user's id: it's `profile.owner_id` if the active profile is user-owned,
else `None`. This means two different users both using the same *template* profile
still share memory exactly as today; only a user's own cloned/private profile gets
private memory. Resolve this once per session (`owner_user_id = profile.owner_id if
profile else None`) and pass it through both call sites.

`GET/DELETE /v1/sessions*` (`api/routes/sessions.py`) gain a `request: Request` param;
non-admin callers get an implicit `user_id=<themselves>` filter added to every
`session_store` call in addition to the existing `profile` filter; admin callers are
unfiltered (unchanged).

## Component 3 — `model_registry_entries` table

```python
class ModelRegistryEntry(Base):
    __tablename__ = "model_registry_entries"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(8))       # "stt" | "tts" | "llm"
    engine: Mapped[str] = mapped_column(String(64))
    model_id: Mapped[str] = mapped_column(String(128))
    label: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    stage: Mapped[str] = mapped_column(String(16), default="stable")  # "stable" | "testing"
```
Unique index on `(kind, engine, model_id)`. A brand-new table — no migration concern,
`create_all` covers it.

**Seeding** (idempotent, run once at startup alongside the other seed calls in
`main.py`'s `lifespan`): for each `(engine, registry)` in `STT_MODEL_REGISTRIES`, for
each `m` in `registry.list_models()`, upsert
`(kind="stt", engine=engine, model_id=m["id"], label=m["label"])` if that
`(kind, engine, model_id)` doesn't already exist (never overwrite an admin's `enabled`/
`stage` edit on a re-seed). For each `engine_name` in `tts_service.providers.keys()`,
upsert `(kind="tts", engine=engine_name, model_id=engine_name, label=engine_name)` —
TTS gates at engine-selection granularity (no per-voice variant concept exists today).
No LLM auto-seed.

**Validation gate** — a new function
`apps/api_gateway/app/services/model_registry/gate.py::check_model_allowed(kind, engine, model_id, user) -> None`,
raising a new `ModelNotAllowedError(AppError)` (`status_code = 403`, added to
`apps/api_gateway/app/core/errors.py` alongside the other named `AppError`
subclasses) — distinct from the existing plain `AppError` (400) that
`registry.validate()` raises for "this model id doesn't exist at all," since this is a
permission/authorization concern, not a data-validation one. Looks up the matching
entry; if none, return (unrestricted); if found and not `enabled`, raise; if
`stage == "testing"` and not `user.can_use_testing`, raise. Called from:
- `apps/api_gateway/app/api/routes/profiles.py`'s `_validate_stt_model` (existing
  function, extended) — after the existing `registry.validate()` existence check,
  additionally call `check_model_allowed("stt", engine, profile.stt.model, user)`, and
  add a new `check_model_allowed("llm", profile.llm.engine, profile.llm.model, user)`
  call (no-op if `profile.llm.engine` is blank).
- `apps/api_gateway/app/api/routes/tts_profiles.py`'s create/update handlers — add
  `check_model_allowed("tts", tts_profile.engine, tts_profile.engine, user)` (model_id
  == engine, per the seeding grain above).

This requires both route handlers to gain a `request: Request` param (to resolve the
acting `User` via `user_store.get_by_id(request.session["user_id"])`) — already true
for `profiles.py` once Component 1's ownership routes land in the same file; new for
`tts_profiles.py`.

## Component 4 — admin `POST /v1/model_registry` (blocking test-before-add)

```python
class CreateModelRegistryEntryRequest(BaseModel):
    kind: str            # "stt" | "tts" | "llm"
    engine: str
    model_id: str
    label: str
    stage: str = "stable"
    # test parameters, kind-dependent:
    base_url: str = ""   # llm
    api_key: str = ""    # llm
    sample_text: str = "xin chào"  # tts test phrase, llm test message
```

Handler runs the real test call matching `kind` *before* any DB write:
- `"stt"`: `await stt_service.get_provider(engine).transcribe_bytes(_SAMPLE_PCM16)`
  (`_SAMPLE_PCM16` a short constant silence/tone buffer module-level in the route
  file, same shape as existing test fixtures use, e.g. `b"\x00\x00" * 1600`).
- `"tts"`: `await tts_service.get_provider(engine).synthesize(TTSRequest(text=sample_text, engine=engine))`.
- `"llm"`: `await OpenAICompatResponder(base_url, api_key, model_id, system_prompt="", timeout=settings.conversation_llm_timeout_seconds).reply([{"role": "user", "content": sample_text}])`.

Any exception from the provider call is caught and returned as `400 {"detail": str(exc)}`
— no row is written. On success, `ModelRegistryEntry` is inserted (id=uuid4(),
enabled=True by construction — an admin just proved it works, no reason to force a
second manual enable step). `PATCH /v1/model_registry/{id} {enabled?, stage?}` and
`GET /v1/model_registry` round out the admin surface — no re-test on toggle, since
toggling enabled/stage isn't claiming the model works, just controlling exposure.

The entire `/v1/model_registry` prefix (`POST`, `GET`, `PATCH`) is admin-only —
`AuthGuardMiddleware`'s `_ADMIN_PREFIXES` tuple (`apps/api_gateway/app/core/auth_guard.py`)
gains `"/v1/model_registry"` alongside the existing `/v1/system`, `/v1/models`,
`/v1/users`, `/v1/devices` entries.

## Component 5 — UI

**Profiles/TtsProfiles/MCP servers pages** (existing Chat-tab profile panel,
`mcp-servers.js`): each row gains a "Clone" action alongside existing edit/delete,
opening a small prompt for the new name, then `POST .../{name}/clone`. Own private
rows get a visual marker (e.g. a small "mine" badge) distinguishing them from admin
templates in the same list — reusing the existing `.hint`/badge-style classes, no new
CSS. Non-owned rows a user can see (templates) show Clone but not Edit/Delete (only
admins edit templates; the 404-on-forbidden-action server response is the real
boundary either way, but hiding the buttons avoids a pointless round trip).

**New "Model Registry" admin page** (nav item alongside Users/Devices, admin-only,
same `.admin-only` pattern): a table (kind, engine, model_id, label, enabled toggle,
stage select) plus an "Add entry" form matching `CreateModelRegistryEntryRequest`
(fields shown/hidden by `kind` selection — `base_url`/`api_key` only for `kind=llm`).
Submitting shows a "Testing…" state (the call is synchronous and may take several
seconds for a real LLM/TTS round trip) before success/failure.

## Migration / rollout notes

- `Profile`/`TtsProfile`/`McpServer`: zero-downtime, additive Pydantic field — no
  action needed on deploy.
- `ChatSession`/`MemoryItem`/`MemoryProfileDoc`: the `_ensure_column` startup check
  handles both a fresh DB (columns present from `create_all`) and an existing DB
  (columns added via `ALTER TABLE` on first boot after this lands) — no manual
  operator step.
- `model_registry_entries`: new table, auto-seeded on first boot after this lands.
  Existing profiles referencing STT models not yet in the registry at all (shouldn't
  happen, since seeding covers every model the registries already know about) fall
  through the "no matching entry = unrestricted" rule harmlessly.

## Testing plan

- Unit: ownership filtering/visibility on list/get/update/delete/clone for all three
  config-store resources (own-row, template, other-user's-row-hidden, name-collision
  on clone); `_ensure_column` idempotency (run twice, no error, column present);
  session/memory `user_id` resolution from profile ownership (template vs. owned
  profile); `check_model_allowed` for enabled/disabled/testing-gated/no-match cases;
  the registry-entry test-before-add endpoint for both a passing and a failing
  provider call (stub providers, not real network calls in tests).
- Integration: full WS conversation flow confirms a created session/memory row
  carries the expected `user_id` (or `None` for a template profile); `/v1/sessions`
  scoping for a non-admin vs. admin caller.
- Manual: admin adds a real LLM registry entry against a stub/local endpoint, confirms
  the blocking test succeeds/fails appropriately; a `can_use_testing=False` user is
  rejected when saving a profile pointing at a `stage=testing` model; a `can_use_testing=True`
  user succeeds.
