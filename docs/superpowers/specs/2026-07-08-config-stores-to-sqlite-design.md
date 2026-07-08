# Config stores → SQLite (Postgres-ready) — design

**Date:** 2026-07-08
**Status:** Approved (design)

## Problem & goal
Runtime data (sessions, messages, memories) is already in SQLite via async SQLAlchemy and Postgres-ready (swap `settings.database_url`). But the four **config/registry stores** are still JSON files:
`profiles.json` (ProfileStore), `tts_profiles.json` (TtsProfileStore), `mcp_servers.json` (McpServerStore), `system_config.json` (SystemConfigStore).
Goal: move all four into the DB so the **whole app** is Postgres-ready via one `DATABASE_URL`, with **zero change to the ~30 call sites** that use the stores' synchronous API.

## Key decisions (approved)
1. **Keep the sync store API** (`list/get/upsert/delete`, `SystemConfig get/set_base_context`). Back each store with an in-memory cache + write-through to the DB. No caller becomes `async`. (Config is small and rarely written; sync DB access is fine.)
2. **Sync SQLAlchemy engine** for config, separate from the async engine used by sessions/memories, pointing at the **same** DB (URL derived from `settings.database_url` by swapping the async driver for the sync one).
3. **One-time import then delete JSON.** On boot, per store: create table (idempotent) → if the table is empty and the legacy JSON file exists, import each record into the DB, then **delete** the JSON file. Afterwards the DB is the sole source of truth; config is edited via UI/API, not files.
4. **"name + JSON blob" schema** — minimal columns, keeps the Pydantic model as the contract, tolerates model evolution (e.g. `Profile.session`) without column migrations.

## Schema
Sync SQLAlchemy Base (separate metadata from the async `Base`), tables in the same DB:
- `config_profiles(name TEXT PK, data TEXT/JSON)` → `Profile`
- `config_tts_profiles(name TEXT PK, data)` → `TtsProfile`
- `config_mcp_servers(name TEXT PK, data)` → `McpServer`
- `config_system(id INTEGER PK = 1, data)` → `SystemConfig` (singleton row)

`data` holds `model.model_dump_json()`; read path is `Model.model_validate_json(data)`. On Postgres the column can be `JSONB`; on SQLite it's `TEXT`. Use SQLAlchemy `JSON` type for portability, or `Text` storing the JSON string (chosen: `Text` storing the Pydantic JSON string — simplest, driver-agnostic, and the models already round-trip through JSON strings today).

## Sync engine (`app/services/db/sync_engine.py`)
- `sync_database_url(async_url: str) -> str`: `sqlite+aiosqlite:///X` → `sqlite:///X`; `postgresql+asyncpg://…` → `postgresql+psycopg://…`; passthrough if already sync.
- Lazy `create_engine` + `sessionmaker`, `configure(url=None)` (tests pass a tmp path, mirroring the async engine), `init_config_tables()` (create_all on the config Base, idempotent).

## Store base (`SqliteBackedStore`)
Generic base parameterized by (table, Pydantic model, key attribute):
- In-memory `dict[str, Model]` cache.
- `load()`: `init_config_tables()`; read all rows into cache; if cache empty and legacy JSON path exists → import from JSON (parse with the model), upsert to DB + cache, then `os.remove` the JSON file (log the migration).
- `list() -> dict[str, Model]`: copy of cache.
- `get(name) -> Model | None`: cache lookup.
- `upsert(model)`: write-through (DB `INSERT … ON CONFLICT`/merge) + cache.
- `delete(name)`: DB delete + cache pop.
All synchronous. A module-level lock guards writes.

`SystemConfigStore` is a thin singleton variant (`get()`, `set_base_context(value)`), one row `id=1`.

## Wiring
- Keep the existing module-level singletons and names (`profile_store`, `tts_profile_store`, `mcp_server_store`, `system_config_store`) — swap their class to the sqlite-backed implementation with identical method signatures.
- Call each store's `load()` once during app startup (lifespan, after `init_db()`), and in the test DB fixture. Stores must also self-`load()` lazily on first access so unit tests that touch a store without the lifespan still work.

## Dependencies
Add `psycopg[binary]` (sync Postgres driver) alongside the existing `asyncpg`. SQLite needs no new dependency (stdlib driver).

## Testing
- `sync_database_url`: sqlite + postgres + already-sync passthrough.
- Per store: start-empty CRUD round-trip; import-from-JSON-then-delete (tmp JSON present → migrated into DB, cache populated, file removed); no re-import when the table already has rows; cache/DB consistency after upsert/delete across a fresh store instance (persistence).
- `SystemConfig`: default when empty, `set_base_context` persists and survives a new instance.
- Regression gate: the full existing suite stays green (call sites unchanged), including the profiles/mcp/tts/system_config route tests and the Lugo/conversation tests that read `profile_store`.

## Non-goals
- No change to the async data engine (sessions/messages/memories).
- No caller signature changes.
- No admin UI change (the UI already CRUDs via the routes, which call the same store API).
