import pytest
from sqlalchemy import text

from app.services.db.engine import _ensure_column, db_session


@pytest.mark.asyncio
async def test_ensure_column_adds_missing_column_once():
    async with db_session() as s:
        await s.execute(text("CREATE TABLE IF NOT EXISTS _ensure_column_test (id VARCHAR(36) PRIMARY KEY)"))
        await s.commit()
        conn = await s.connection()
        await _ensure_column(conn, "_ensure_column_test", "extra", "VARCHAR(36)")
        await _ensure_column(conn, "_ensure_column_test", "extra", "VARCHAR(36)")  # idempotent, no error
        result = await s.execute(text("PRAGMA table_info(_ensure_column_test)"))
        columns = {row[1] for row in result.fetchall()}
        assert "extra" in columns
