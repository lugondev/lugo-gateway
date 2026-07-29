"""Async DB engine + session factory.

SQLite (aiosqlite) by default; PostgreSQL later is a settings.database_url
change. Tables are created lazily on first use so tests and the app need no
explicit startup hook.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.settings import settings

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
    if url.startswith("sqlite"):
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
    """NULL user_id -> '' so rows land in the shared-device bucket and match
    the composite-key filters. Idempotent."""
    await conn.exec_driver_sql(f"UPDATE {table} SET user_id = '' WHERE user_id IS NULL")


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
    await conn.exec_driver_sql("ALTER TABLE memory_profile_docs RENAME TO _mpd_old")
    await conn.exec_driver_sql(
        "CREATE TABLE memory_profile_docs ("
        "user_id VARCHAR(36) NOT NULL DEFAULT '', "
        "profile_id VARCHAR(128) NOT NULL, "
        "content TEXT DEFAULT '', "
        "updated_at DATETIME, "
        "PRIMARY KEY (user_id, profile_id))"
    )
    await conn.exec_driver_sql(
        "INSERT INTO memory_profile_docs (user_id, profile_id, content, updated_at) "
        "SELECT COALESCE(user_id, ''), profile_id, content, updated_at FROM _mpd_old"
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
            await _backfill_null_user_ids(conn, "memories")
            await _ensure_doc_composite_pk(conn)
        _initialized = True


@asynccontextmanager
async def db_session():
    """Async context manager yielding an AsyncSession, init-on-first-use."""
    if not _initialized:
        await init_db()
    assert _factory is not None
    async with _factory() as session:
        yield session
