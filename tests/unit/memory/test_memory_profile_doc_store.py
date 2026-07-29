import pytest

from app.services.memory.store import profile_doc_store


@pytest.mark.asyncio
async def test_get_absent_returns_none():
    assert await profile_doc_store.get("ghost") is None


@pytest.mark.asyncio
async def test_upsert_creates_then_updates():
    created = await profile_doc_store.upsert("pet", "## User Profile\n- v1")
    assert created["content"] == "## User Profile\n- v1"
    got = await profile_doc_store.get("pet")
    assert got["content"] == "## User Profile\n- v1"

    updated = await profile_doc_store.upsert("pet", "## User Profile\n- v2")
    assert updated["content"] == "## User Profile\n- v2"
    assert (await profile_doc_store.get("pet"))["content"] == "## User Profile\n- v2"


@pytest.mark.asyncio
async def test_delete():
    await profile_doc_store.upsert("pet", "x")
    assert await profile_doc_store.delete("pet") is True
    assert await profile_doc_store.delete("pet") is False
    assert await profile_doc_store.get("pet") is None


async def test_doc_is_scoped_by_user():
    from app.services.memory.store import profile_doc_store

    await profile_doc_store.upsert("shared", "A's profile", user_id="user-a")
    await profile_doc_store.upsert("shared", "B's profile", user_id="user-b")

    a = await profile_doc_store.get("shared", user_id="user-a")
    b = await profile_doc_store.get("shared", user_id="user-b")
    assert a["content"] == "A's profile"
    assert a["user_id"] == "user-a"
    assert b["content"] == "B's profile"


async def test_doc_none_user_uses_empty_bucket():
    from app.services.memory.store import profile_doc_store

    await profile_doc_store.upsert("dev", "device doc", user_id=None)
    got = await profile_doc_store.get("dev", user_id="")
    assert got["content"] == "device doc"
    assert await profile_doc_store.delete("dev", user_id="") is True
    assert await profile_doc_store.get("dev", user_id="") is None
