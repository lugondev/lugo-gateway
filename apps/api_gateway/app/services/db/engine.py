"""Async DB engine + session factory.

SQLite (aiosqlite) by default; PostgreSQL later is a settings.database_url
change. Tables are created lazily on first use so tests and the app need no
explicit startup hook.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.settings import settings

# How long a connection waits for a lock before giving up with "database is
# locked". SQLite serializes writers, and this process has TWO engines on the
# same file (this one, plus the synchronous one the config stores use), so
# contention is normal rather than exceptional.
_BUSY_TIMEOUT_MS = 10_000


def apply_sqlite_pragmas(dbapi_connection) -> None:
    """Per-connection SQLite setup. Applied to BOTH engines (this module and
    db/sync_engine.py) -- they open the same file, so a pragma set on one side
    only would leave the other with the defaults.

    Why each one:

    * journal_mode=WAL -- the default rollback journal makes a writer block
      every reader and vice versa. On a voice turn that means a usage row being
      written can stall the session lookup of a device connecting at the same
      moment. WAL is a persistent property of the database FILE, so setting it
      repeatedly is a no-op after the first time; it is set per connection
      anyway because a fresh file (tests, first boot) needs it too.
    * busy_timeout -- without it a contended write raises "database is locked"
      immediately instead of waiting for the other writer to finish.
    * synchronous=NORMAL -- safe under WAL (the WAL itself is still durable
      across process crashes; only an OS-level crash can lose the last commits)
      and removes an fsync from every single commit. Every turn writes usage
      rows and messages, so this is on the hot path.

    Deliberately NOT set: foreign_keys=ON. The schema has FKs
    (messages -> sessions, devices -> users) that have never been enforced, and
    turning enforcement on under existing data is a data-migration question, not
    a pragma.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()

_engine: AsyncEngine | None = None
_factory: async_sessionmaker[AsyncSession] | None = None
_init_lock = asyncio.Lock()
_initialized = False


def configure(url: str | None = None) -> None:
    """(Re)point the DB at a URL. Tests pass a tmp path; prod uses settings.

    Each test's pytest-asyncio run gets its own event loop, and the aiosqlite
    connections opened against the *previous* engine are still tied to the
    *previous* (now-closed) loop. `sync_engine.dispose()` alone doesn't close
    those async connections properly, leaving a stale one for the garbage
    collector to finalize later -- sometimes mid-query in a later test, which
    manifests as a hang rather than a clean error. Dispose the old engine
    async (`asyncio.run` is safe here: this is only ever called from a plain
    sync fixture, never from inside a running loop) so no connection survives
    into the next test's loop.
    """
    global _engine, _factory, _initialized, _init_lock
    if _engine is not None:
        try:
            asyncio.run(_engine.dispose())
        except RuntimeError:
            # Already inside a running loop somehow -- best-effort fallback.
            _engine.sync_engine.dispose()
    url = url or settings.database_url_resolved
    engine_kwargs: dict = {}
    is_sqlite = url.startswith("sqlite")
    if is_sqlite:
        db_file = url.split("///", 1)[-1]
        if db_file and db_file != ":memory:":
            Path(db_file).parent.mkdir(parents=True, exist_ok=True)
        # NEVER pool aiosqlite connections: each one is bound to the event
        # loop that created it, and this process runs many loops (pytest's
        # per-test loops, TestClient portals, asyncio.run() in sync test
        # helpers). A pooled connection checked out under a different loop
        # wedges forever mid-await -- observed as WS tests hanging at
        # TestClient portal teardown while a watchdog awaited get_by_id() on
        # a connection created by an earlier asyncio.run(). Opening a local
        # SQLite file per session is microseconds; pooling buys nothing here.
        from sqlalchemy.pool import NullPool

        engine_kwargs["poolclass"] = NullPool
    _engine = create_async_engine(url, **engine_kwargs)
    if is_sqlite:
        # On sync_engine, not the AsyncEngine: the "connect" event fires on the
        # DBAPI layer, which for aiosqlite is SQLAlchemy's sync-facade adapter.
        @event.listens_for(_engine.sync_engine, "connect")
        def _on_connect(dbapi_connection, _record):  # pragma: no cover - trivial
            apply_sqlite_pragmas(dbapi_connection)

    _factory = async_sessionmaker(_engine, expire_on_commit=False)
    _initialized = False
    # A fresh lock too -- an asyncio.Lock first acquired under a now-closed
    # event loop raises "bound to a different event loop" if reused under a
    # new one (see asyncio.mixins._LoopBoundMixin).
    _init_lock = asyncio.Lock()


def get_engine() -> AsyncEngine:
    if _engine is None:
        configure()
    assert _engine is not None
    return _engine


async def _ensure_column(conn, table: str, column: str, ddl_type: str) -> None:
    """Idempotent ALTER TABLE ADD COLUMN -- this codebase has no migration
    framework, and Base.metadata.create_all only creates missing tables, never
    alters existing ones. Safe to call every startup."""
    result = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
    existing = {row[1] for row in result.fetchall()}
    if column not in existing:
        await conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")


async def _backfill_null_user_ids(conn, table: str) -> None:
    """NULL user_id -> the ownerless subject, so rows are readable at all.

    A NULL is silently excluded by every `user_id == <subject>` scoped query, so
    a row left NULL is a row nothing can ever find. It lands on ANON_SUBJECT
    rather than DEV_SUBJECT for the same reason the '' migration does: nothing
    recorded which case wrote it, and treating old data as real speech is the
    conservative error. Idempotent.
    """
    from app.services.memory.subjects import ANON_SUBJECT

    await conn.exec_driver_sql(
        f"UPDATE {table} SET user_id = ? WHERE user_id IS NULL", (ANON_SUBJECT,)
    )


async def _ensure_doc_composite_pk(conn) -> None:
    """Rebuild memory_profile_docs under PK (user_id, profile_id) if it still
    has the legacy single-column PK. SQLite cannot ALTER a primary key, so
    rename-copy-drop. Idempotent: no-op once user_id is already part of the PK."""
    info = await conn.exec_driver_sql("PRAGMA table_info(memory_profile_docs)")
    rows = info.fetchall()
    if not rows:
        # Defensive only: unreachable from init_db(), where create_all always
        # creates this table first. Guards direct unit-test invocation of
        # this helper against a missing table.
        return
    pk_cols = {r[1] for r in rows if r[5]}  # r[5] = pk position, nonzero => PK member
    if "user_id" in pk_cols:
        return  # already migrated
    from app.services.memory.subjects import ANON_SUBJECT

    await conn.exec_driver_sql("ALTER TABLE memory_profile_docs RENAME TO _mpd_old")
    await conn.exec_driver_sql(
        "CREATE TABLE memory_profile_docs ("
        f"user_id VARCHAR(36) NOT NULL DEFAULT '{ANON_SUBJECT}', "
        "profile_id VARCHAR(128) NOT NULL, "
        "content TEXT DEFAULT '', "
        "updated_at DATETIME, "
        "PRIMARY KEY (user_id, profile_id))"
    )
    await conn.exec_driver_sql(
        "INSERT INTO memory_profile_docs (user_id, profile_id, content, updated_at) "
        f"SELECT COALESCE(user_id, '{ANON_SUBJECT}'), profile_id, content, updated_at "
        "FROM _mpd_old"
    )
    await conn.exec_driver_sql("DROP TABLE _mpd_old")


async def init_db() -> None:
    """Create tables once (idempotent, concurrency-safe)."""
    from app.services.db.models import Base

    global _initialized
    if _factory is None:
        configure()
    async with _init_lock:
        if _initialized:
            return
        assert _engine is not None
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await _ensure_column(conn, "sessions", "user_id", "VARCHAR(36)")
            await _ensure_column(conn, "memories", "user_id", "VARCHAR(36)")
            await _ensure_column(conn, "memory_profile_docs", "user_id", "VARCHAR(36)")
            await _ensure_column(conn, "model_registry_entries", "api_key", "VARCHAR(256) DEFAULT ''")
            await _ensure_column(conn, "model_registry_entries", "base_url", "VARCHAR(256) DEFAULT ''")
            await _ensure_column(conn, "model_registry_entries", "config", "JSON DEFAULT '{}'")
            await _ensure_column(conn, "model_registry_entries", "is_default", "BOOLEAN DEFAULT 0")
            await _ensure_column(conn, "devices", "profile_id", "VARCHAR(128) DEFAULT ''")
            await _ensure_column(conn, "sessions", "source", "VARCHAR(16) DEFAULT ''")
            await _ensure_column(conn, "sessions", "client_id", "VARCHAR(64) DEFAULT ''")
            # create_all creates missing TABLES, never indexes on an existing
            # one, so an index added to a model after first boot needs this.
            # "IF NOT EXISTS" is valid on both SQLite and PostgreSQL.
            await conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_sessions_created_at ON sessions (created_at)"
            )
            await _backfill_null_user_ids(conn, "memories")
            await _ensure_doc_composite_pk(conn)
        _initialized = True


async def dispose_engine() -> None:
    """Close every pooled DB connection. For application shutdown -- without it
    the interpreter tore them down at exit instead, which on SQLite means the
    WAL is checkpointed (or not) by whatever ran last."""
    global _initialized
    if _engine is None:
        return
    await _engine.dispose()
    _initialized = False


@asynccontextmanager
async def db_session():
    """Async context manager yielding an AsyncSession, init-on-first-use."""
    if not _initialized:
        await init_db()
    assert _factory is not None
    async with _factory() as session:
        yield session
