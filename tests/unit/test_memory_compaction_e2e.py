import pytest

from app.services.memory.compactor import MemoryCompactor
from app.services.memory.extractor import MemoryExtractor
from app.services.memory.store import memory_store, profile_doc_store
from app.services.history.store import session_store
from app.services.profiles.models import Profile


@pytest.mark.asyncio
async def test_extract_then_compact_without_embed_model(monkeypatch):
    await session_store.create("e2e", profile_id="u")
    await session_store.append_message("e2e", 1, "user", "hello")
    await session_store.append_message("e2e", 1, "assistant", "hi")

    async def fake_extract(self, messages, base_url, api_key, model, **kwargs):
        return ["User is Toan", "User builds an ESP32 assistant", "User speaks Vietnamese"]

    async def fake_call(self, profile, current_doc, facts, user_id=None):
        assert "User is Toan" in "\n".join(facts)
        return "## User Profile\n### Danh tính\n- Toan, speaks Vietnamese\n### Dự án\n- ESP32 assistant"

    monkeypatch.setattr(MemoryExtractor, "extract", fake_extract)
    monkeypatch.setattr(MemoryCompactor, "_call_llm", fake_call)

    profile = Profile(
        name="u",
        llm={"base_url": "http://llm.local/v1", "model": "m"},
        memory={"compaction_threshold": 3},  # no embed_model
    )
    added = await MemoryExtractor().extract_and_upsert("e2e", profile)
    assert added == 3
    # buffer hit the threshold -> compacted and pruned
    assert await memory_store.list("u") == []
    doc = await profile_doc_store.get("u")
    assert doc["content"].startswith("## User Profile")
    assert "Toan" in doc["content"]
