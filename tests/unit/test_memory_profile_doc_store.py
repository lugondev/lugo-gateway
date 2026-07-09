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
