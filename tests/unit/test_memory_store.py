import pytest

from app.services.memory.store import MemoryStore


@pytest.fixture
def store():
    return MemoryStore()


@pytest.mark.asyncio
async def test_add_and_list(store):
    m = await store.add("pet", "User prefers Vietnamese", source_session_id="s1")
    assert m["id"]
    rows = await store.list("pet")
    assert len(rows) == 1
    assert rows[0]["content"] == "User prefers Vietnamese"
    assert rows[0]["source_session_id"] == "s1"
    assert await store.list("other") == []


@pytest.mark.asyncio
async def test_update_and_delete(store):
    m = await store.add("pet", "old")
    updated = await store.update(m["id"], "new")
    assert updated["content"] == "new"
    assert await store.update("ghost", "x") is None
    assert await store.delete(m["id"]) is True
    assert await store.delete(m["id"]) is False


@pytest.mark.asyncio
async def test_delete_all(store):
    await store.add("pet", "a")
    await store.add("pet", "b")
    await store.add("other", "c")
    assert await store.delete_all("pet") == 2
    assert await store.list("pet") == []
    assert len(await store.list("other")) == 1


@pytest.mark.asyncio
async def test_embedding_persists(store):
    m = await store.add("pet", "fact", embedding=[0.5, 0.5])
    rows = await store.list("pet")
    assert rows[0]["embedding"] == [0.5, 0.5]
