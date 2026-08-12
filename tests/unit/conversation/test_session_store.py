from datetime import datetime, timedelta

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
async def test_messages_carry_an_explicitly_utc_timestamp(store):
    """The web client renders per-message clock times.

    The offset must be in the string: SQLite drops tzinfo, and JS reads a naive
    ISO string as LOCAL time -- so a bare timestamp shows the wrong hour to
    anyone not on UTC.
    """
    await store.create("s1")
    await store.append_message("s1", 1, "user", "xin chào")
    (msg,) = await store.get_messages("s1")
    assert msg["created_at"] is not None
    parsed = datetime.fromisoformat(msg["created_at"])
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


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


@pytest.mark.asyncio
async def test_delete_many_counts_only_existing(store):
    await store.create("a")
    await store.append_message("a", 1, "user", "hi")
    await store.create("b")
    deleted = await store.delete_many(["a", "b", "ghost"])
    assert deleted == 2
    assert await store.get("a") is None
    assert await store.get("b") is None
    assert await store.get_messages("a") == []


@pytest.mark.asyncio
async def test_delete_many_empty_list_is_noop(store):
    await store.create("a")
    assert await store.delete_many([]) == 0
    assert await store.exists("a") is True


@pytest.mark.asyncio
async def test_clear_only_empty_keeps_non_empty(store):
    await store.create("full")
    await store.append_message("full", 1, "user", "x")
    await store.create("empty1")
    await store.create("empty2")
    deleted = await store.clear(profile_id=None, only_empty=True)
    assert deleted == 2
    assert await store.exists("full") is True
    assert await store.exists("empty1") is False
    assert await store.exists("empty2") is False


@pytest.mark.asyncio
async def test_clear_all_respects_profile_scope(store):
    await store.create("p1a", profile_id="p1")
    await store.append_message("p1a", 1, "user", "x")
    await store.create("p1b", profile_id="p1")
    await store.create("p2a", profile_id="p2")
    deleted = await store.clear(profile_id="p1", only_empty=False)
    assert deleted == 2
    assert await store.exists("p1a") is False
    assert await store.exists("p1b") is False
    assert await store.exists("p2a") is True


@pytest.mark.asyncio
async def test_clear_all_none_profile_clears_everything(store):
    await store.create("a", profile_id="p1")
    await store.create("b", profile_id="p2")
    deleted = await store.clear(profile_id=None, only_empty=False)
    assert deleted == 2
    assert len(await store.list()) == 0


@pytest.mark.asyncio
async def test_clear_only_empty_scoped_to_profile(store):
    await store.create("p1empty", profile_id="p1")
    await store.create("p2empty", profile_id="p2")
    deleted = await store.clear(profile_id="p1", only_empty=True)
    assert deleted == 1
    assert await store.exists("p1empty") is False
    assert await store.exists("p2empty") is True


@pytest.mark.asyncio
async def test_create_with_user_id_and_filter(store):
    await store.create("s1", profile_id="pet", user_id="user-a")
    await store.create("s2", profile_id="pet", user_id="user-b")
    rows = await store.list(user_id="user-a")
    assert [r["id"] for r in rows] == ["s1"]
    got = await store.get("s1")
    assert got["user_id"] == "user-a"


@pytest.mark.asyncio
async def test_count_matches_list_and_filters_by_user(store):
    await store.create("s1", user_id="u1")
    await store.create("s2", user_id="u1")
    await store.create("s3", user_id="u2")

    assert await store.count() == 3
    assert await store.count(user_id="u1") == 2
    assert await store.count(user_id="u2") == 1
    assert await store.count(user_id="nobody") == 0
    assert await store.count(user_id="u1") == len(await store.list(user_id="u1"))
