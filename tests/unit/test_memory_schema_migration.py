"""A DB created under the OLD single-PK doc schema must, after init_db,
carry every legacy doc under user_id='' and accept per-user docs on one
profile without a PK collision."""

import pytest

from app.services.db import engine as db_engine


@pytest.fixture
async def old_schema_db(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 'old.db'}"
    # configure() only disposes a pre-existing engine cleanly when called
    # from a plain sync context (see its docstring): it wraps the dispose
    # in asyncio.run(), which can't run inside this fixture's already-active
    # loop. In that case it falls back to a sync dispose, but the coroutine
    # object eagerly built for asyncio.run()'s argument is discarded
    # unawaited and leaks a RuntimeWarning at GC time. Dispose the
    # conftest-installed engine ourselves first so configure() sees a clean
    # slate (_engine is None) and skips that path entirely.
    if db_engine._engine is not None:
        await db_engine._engine.dispose()
        db_engine._engine = None
    db_engine.configure(url)
    # Simulate a pre-migration DB: single-PK doc table + a NULL-user memory row.
    eng = db_engine.get_engine()
    async with eng.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE memory_profile_docs ("
            "profile_id VARCHAR(128) PRIMARY KEY, content TEXT, updated_at DATETIME)"
        )
        await conn.exec_driver_sql(
            "INSERT INTO memory_profile_docs (profile_id, content, updated_at) "
            "VALUES ('legacy', 'old doc', '2026-01-01 00:00:00')"
        )
        await conn.exec_driver_sql(
            "CREATE TABLE memories (id VARCHAR(36) PRIMARY KEY, profile_id VARCHAR(128), "
            "content TEXT, source_session_id VARCHAR(36), embedding JSON, "
            "created_at DATETIME, updated_at DATETIME)"
        )
        await conn.exec_driver_sql(
            "INSERT INTO memories (id, profile_id, content, created_at, updated_at) "
            "VALUES ('m1', 'legacy', 'device fact', '2026-01-01', '2026-01-01')"
        )
    # Reset the init guard so init_db re-runs against this DB.
    db_engine._initialized = False
    try:
        yield url
    finally:
        db_engine._initialized = False
        # init_db() reuses this same engine (it only reconfigures when
        # _factory is None), so this is the one and only engine to dispose.
        await eng.dispose()


async def test_migration_backfills_and_rebuilds(old_schema_db):
    from app.services.memory.store import memory_store, profile_doc_store

    await db_engine.init_db()

    # memories NULL user backfilled to ''
    assert [m["content"] for m in await memory_store.list("legacy", user_id="")] == ["device fact"]

    # legacy doc preserved under ''
    assert (await profile_doc_store.get("legacy", user_id=""))["content"] == "old doc"

    # composite PK now allows two users on one profile
    await profile_doc_store.upsert("legacy", "A doc", user_id="user-a")
    await profile_doc_store.upsert("legacy", "B doc", user_id="user-b")
    assert (await profile_doc_store.get("legacy", user_id="user-a"))["content"] == "A doc"
    assert (await profile_doc_store.get("legacy", user_id="user-b"))["content"] == "B doc"


async def test_migration_is_idempotent(old_schema_db):
    await db_engine.init_db()
    db_engine._initialized = False
    await db_engine.init_db()  # second run must not raise or duplicate
    from app.services.memory.store import profile_doc_store
    assert (await profile_doc_store.get("legacy", user_id=""))["content"] == "old doc"
