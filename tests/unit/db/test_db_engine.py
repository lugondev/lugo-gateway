import pytest

from app.services.db import engine as db_engine
from app.services.db.models import ChatMessage, ChatSession, MemoryItem


@pytest.mark.asyncio
async def test_db_session_creates_tables_lazily():
    async with db_engine.db_session() as s:
        sess = ChatSession(id="s1", profile_id="p1", meta={"a": 1})
        s.add(sess)
        await s.commit()
    async with db_engine.db_session() as s:
        got = await s.get(ChatSession, "s1")
        assert got is not None
        assert got.profile_id == "p1"
        assert got.meta == {"a": 1}
        assert got.ended_at is None


@pytest.mark.asyncio
async def test_message_and_memory_models_roundtrip():
    async with db_engine.db_session() as s:
        s.add(ChatSession(id="s2", profile_id=""))
        s.add(ChatMessage(session_id="s2", turn=1, role="user", content="hi"))
        s.add(MemoryItem(id="m1", profile_id="p1", content="fact", embedding=[0.1, 0.2]))
        await s.commit()
    async with db_engine.db_session() as s:
        mem = await s.get(MemoryItem, "m1")
        assert mem.embedding == [0.1, 0.2]
        assert mem.created_at is not None
