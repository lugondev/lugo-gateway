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
    await store.add("pet", "fact", embedding=[0.5, 0.5])
    rows = await store.list("pet")
    assert rows[0]["embedding"] == [0.5, 0.5]


@pytest.mark.asyncio
async def test_delete_many(store):
    a = await store.add("pet", "a")
    b = await store.add("pet", "b")
    c = await store.add("pet", "c")
    assert await store.delete_many([a["id"], b["id"]]) == 2
    remaining = {m["content"] for m in await store.list("pet")}
    assert remaining == {"c"}
    assert await store.delete_many([]) == 0
    _ = c


@pytest.mark.asyncio
async def test_add_with_user_id_roundtrips(store):
    added = await store.add("profile-a", "likes tea", user_id="user-a")
    assert added["user_id"] == "user-a"
    items = await store.list("profile-a", user_id="user-a")
    assert items[0]["user_id"] == "user-a"


@pytest.mark.asyncio
async def test_add_without_user_id_normalizes_at_the_write_boundary(store):
    # A named subject (never None, never '') is what "no attributable user"
    # stores as -- which one depends on auth mode, see services/memory/subjects.py.
    # add() normalizes via _uid() at the write boundary.
    from app.services.memory.store import DEV_SUBJECT

    added = await store.add("profile-a", "likes coffee")
    assert added["user_id"] == DEV_SUBJECT  # tests run with auth disabled


async def test_list_scopes_by_user():
    from app.services.memory.store import memory_store

    await memory_store.add("shared", "a-fact", user_id="user-a")
    await memory_store.add("shared", "b-fact", user_id="user-b")

    a = await memory_store.list("shared", user_id="user-a")
    assert [m["content"] for m in a] == ["a-fact"]
    assert a[0]["user_id"] == "user-a"

    b = await memory_store.list("shared", user_id="user-b")
    assert [m["content"] for m in b] == ["b-fact"]


async def test_none_user_writes_and_reads_the_same_bucket():
    """The invariant that matters: whatever None normalizes to, a later read with
    None must find it. The literal is an implementation detail; a write and a read
    disagreeing is a silently empty memory."""
    from app.services.memory.store import memory_store

    await memory_store.add("dev", "device-fact", user_id=None)
    rows = await memory_store.list("dev", user_id=None)
    assert [m["content"] for m in rows] == ["device-fact"]
    assert rows[0]["user_id"] not in ("", None)


async def test_delete_all_scopes_by_user():
    from app.services.memory.store import memory_store

    await memory_store.add("shared", "a-fact", user_id="user-a")
    await memory_store.add("shared", "b-fact", user_id="user-b")

    deleted = await memory_store.delete_all("shared", user_id="user-a")
    assert deleted == 1
    assert [m["content"] for m in await memory_store.list("shared", user_id="user-b")] == ["b-fact"]


async def test_update_and_delete_reject_wrong_user():
    from app.services.memory.store import memory_store

    row = await memory_store.add("shared", "a-fact", user_id="user-a")
    mid = row["id"]

    assert await memory_store.update(mid, "hax", profile_id="shared", user_id="user-b") is None
    assert await memory_store.delete(mid, profile_id="shared", user_id="user-b") is False
    # owner still can
    assert (await memory_store.update(mid, "fixed", profile_id="shared", user_id="user-a"))["content"] == "fixed"
    assert await memory_store.delete(mid, profile_id="shared", user_id="user-a") is True


async def test_raw_memoryitem_without_user_id_defaults_to_a_readable_subject():
    """Defense-in-depth: a raw MemoryItem insert that bypasses MemoryStore.add
    (which normalizes) must still land on a subject some query can find -- neither
    NULL nor the legacy '', both of which every `== _uid(...)` filter excludes
    silently."""
    import uuid

    from app.services.db.engine import db_session
    from app.services.db.models import MemoryItem

    from app.services.memory.store import ANON_SUBJECT, memory_store

    async with db_session() as s:
        row = MemoryItem(id=str(uuid.uuid4()), profile_id="raw", content="bypass")
        s.add(row)
        await s.commit()
        assert row.user_id == ANON_SUBJECT

    assert [m["content"] for m in await memory_store.list("raw", user_id=ANON_SUBJECT)] == ["bypass"]
