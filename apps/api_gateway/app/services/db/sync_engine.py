"""Synchronous SQLAlchemy engine for the config stores.

Same DB as the async engine (sessions/memories), reached with the sync driver so
the stores' synchronous API needs no await. URL derived from settings.database_url.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.settings import settings

_engine = None
_factory: sessionmaker | None = None
_tables_ready = False


def sync_database_url(async_url: str) -> str:
    if async_url.startswith("sqlite+aiosqlite"):
        return async_url.replace("sqlite+aiosqlite", "sqlite", 1)
    if async_url.startswith("postgresql+asyncpg"):
        return async_url.replace("postgresql+asyncpg", "postgresql+psycopg", 1)
    return async_url  # already sync (or an unknown scheme — pass through)


def configure(url: str | None = None) -> None:
    global _engine, _factory, _tables_ready
    if _engine is not None:
        _engine.dispose()
    sync_url = sync_database_url(url or settings.database_url)
    if sync_url.startswith("sqlite"):
        db_file = sync_url.split("///", 1)[-1]
        if db_file and db_file != ":memory:":
            Path(db_file).parent.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(sync_url, future=True)
    _factory = sessionmaker(_engine, expire_on_commit=False)
    _tables_ready = False


def init_config_tables() -> None:
    global _tables_ready
    if _factory is None:
        configure()
    if _tables_ready:
        return
    from app.services.db.config_models import ConfigBase
    ConfigBase.metadata.create_all(_engine)
    _tables_ready = True


@contextmanager
def session_scope():
    if _factory is None:
        configure()
    s: Session = _factory()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
