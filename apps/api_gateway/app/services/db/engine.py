"""Async DB engine + session factory.

SQLite (aiosqlite) by default; PostgreSQL later is a settings.database_url
change. Tables are created lazily on first use so tests and the app need no
explicit startup hook.
"""

from __future__ import annotations

import asyncio
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
    """(Re)point the DB at a URL. Tests pass a tmp path; prod uses settings."""
    global _engine, _factory, _initialized
    if _engine is not None:
        _engine.sync_engine.dispose()
    url = url or settings.database_url
    if url.startswith("sqlite"):
        db_file = url.split("///", 1)[-1]
        if db_file and db_file != ":memory:":
            Path(db_file).parent.mkdir(parents=True, exist_ok=True)
    _engine = create_async_engine(url)
    _factory = async_sessionmaker(_engine, expire_on_commit=False)
    _initialized = False


async def _ensure_column(conn, table: str, column: str, ddl_type: str) -> None:
    """Idempotent ALTER TABLE ADD COLUMN -- this codebase has no migration
    framework, and Base.metadata.create_all only creates missing tables, never
    alters existing ones. Safe to call every startup."""
    result = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
    existing = {row[1] for row in result.fetchall()}
    if column not in existing:
        await conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")


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
        _initialized = True


@asynccontextmanager
async def db_session():
    """Async context manager yielding an AsyncSession, init-on-first-use."""
    if not _initialized:
        await init_db()
    assert _factory is not None
    async with _factory() as session:
        yield session
