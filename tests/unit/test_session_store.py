import pytest

from app.services.history.store import SessionStore


@pytest.fixture
def store():
    return SessionStore()


@pytest.mark.asyncio
async def test_create_and_get(store):
    await store.create("s1", profile_id="pet", meta={"tts": "vieneu"})
    got = await store.get("s1")
    assert got["profile_id"] == "pet"
    assert got["meta"] == {"tts": "vieneu"}
    assert got["ended_at"] is None
    assert await store.exists("s1") is True
    assert await store.get("nope") is None


@pytest.mark.asyncio
async def test_messages_roundtrip(store):
    await store.create("s1")
    await store.append_message("s1", 1, "user", "xin chào")
    await store.append_message("s1", 1, "assistant", "chào bạn")
    msgs = await store.get_messages("s1")
    assert [(m["role"], m["content"]) for m in msgs] == [
        ("user", "xin chào"),
        ("assistant", "chào bạn"),
    ]


@pytest.mark.asyncio
async def test_list_filters_and_previews(store):
    await store.create("a", profile_id="p1")
    await store.append_message("a", 1, "user", "hello world")
    await store.create("b", profile_id="p2")
    rows = await store.list(profile_id="p1")
    assert len(rows) == 1
    assert rows[0]["id"] == "a"
    assert rows[0]["preview"] == "hello world"
    assert rows[0]["message_count"] == 1
    assert len(await store.list()) == 2


@pytest.mark.asyncio
async def test_mark_ended_and_delete(store):
    await store.create("s1")
    await store.append_message("s1", 1, "user", "x")
    await store.mark_ended("s1")
    assert (await store.get("s1"))["ended_at"] is not None
    assert await store.delete("s1") is True
    assert await store.get("s1") is None
    assert await store.get_messages("s1") == []
    assert await store.delete("s1") is False
