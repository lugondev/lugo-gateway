from app.services.db.sync_engine import sync_database_url


def test_sqlite_async_to_sync():
    assert sync_database_url("sqlite+aiosqlite:///data/app.db") == "sqlite:///data/app.db"


def test_postgres_async_to_sync():
    assert sync_database_url("postgresql+asyncpg://u:p@h/db") == "postgresql+psycopg://u:p@h/db"


def test_already_sync_passthrough():
    assert sync_database_url("sqlite:///x.db") == "sqlite:///x.db"
    assert sync_database_url("postgresql+psycopg://u@h/db") == "postgresql+psycopg://u@h/db"
