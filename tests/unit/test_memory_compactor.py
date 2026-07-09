import pytest

from app.services.memory.compactor import MemoryCompactor
from app.services.memory.store import memory_store, profile_doc_store
from app.services.profiles.models import Profile


def _profile(**mem):
    return Profile(name="pet", llm={"base_url": "http://llm.local/v1", "model": "m"},
                   memory=mem)


@pytest.mark.asyncio
async def test_maybe_compact_below_threshold_noop(monkeypatch):
    await memory_store.add("pet", "a")
    called = {"n": 0}

    async def fake_call(self, profile, current_doc, facts):
        called["n"] += 1
        return "## User Profile\n- x"

    monkeypatch.setattr(MemoryCompactor, "_call_llm", fake_call)
    did = await MemoryCompactor().maybe_compact(_profile(compaction_threshold=5))
    assert did is False
    assert called["n"] == 0
    assert len(await memory_store.list("pet")) == 1


@pytest.mark.asyncio
async def test_compact_rebuilds_doc_and_prunes(monkeypatch):
    ids = [(await memory_store.add("pet", f"fact {i}"))["id"] for i in range(3)]

    async def fake_call(self, profile, current_doc, facts):
        assert "fact 0" in "\n".join(facts)
        return "## User Profile\n### Danh tính\n- merged"

    monkeypatch.setattr(MemoryCompactor, "_call_llm", fake_call)
    did = await MemoryCompactor().maybe_compact(_profile(compaction_threshold=3))
    assert did is True
    doc = await profile_doc_store.get("pet")
    assert doc["content"] == "## User Profile\n### Danh tính\n- merged"
    assert await memory_store.list("pet") == []  # folded facts pruned
    _ = ids


@pytest.mark.asyncio
async def test_compact_preserves_facts_added_after_snapshot(monkeypatch):
    for i in range(3):
        await memory_store.add("pet", f"old {i}")

    async def fake_call(self, profile, current_doc, facts):
        # simulate a concurrent add landing during the LLM call
        await memory_store.add("pet", "new-arrival")
        return "## User Profile\n- merged"

    monkeypatch.setattr(MemoryCompactor, "_call_llm", fake_call)
    await MemoryCompactor().maybe_compact(_profile(compaction_threshold=3))
    remaining = {m["content"] for m in await memory_store.list("pet")}
    assert remaining == {"new-arrival"}


@pytest.mark.asyncio
async def test_compact_llm_failure_keeps_facts(monkeypatch):
    await memory_store.add("pet", "a")
    await memory_store.add("pet", "b")
    await memory_store.add("pet", "c")

    async def boom(self, profile, current_doc, facts):
        raise RuntimeError("llm down")

    monkeypatch.setattr(MemoryCompactor, "_call_llm", boom)
    did = await MemoryCompactor().maybe_compact(_profile(compaction_threshold=3))
    assert did is False
    assert len(await memory_store.list("pet")) == 3  # no data loss
    assert await profile_doc_store.get("pet") is None


@pytest.mark.asyncio
async def test_compact_empty_doc_keeps_facts(monkeypatch):
    await memory_store.add("pet", "a")
    await memory_store.add("pet", "b")
    await memory_store.add("pet", "c")

    async def empty(self, profile, current_doc, facts):
        return "   "

    monkeypatch.setattr(MemoryCompactor, "_call_llm", empty)
    did = await MemoryCompactor().maybe_compact(_profile(compaction_threshold=3))
    assert did is False
    assert len(await memory_store.list("pet")) == 3
