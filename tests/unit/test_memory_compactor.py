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

    async def fake_call(self, profile, current_doc, facts, user_id=None):
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

    async def fake_call(self, profile, current_doc, facts, user_id=None):
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

    async def fake_call(self, profile, current_doc, facts, user_id=None):
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

    async def boom(self, profile, current_doc, facts, user_id=None):
        raise RuntimeError("llm down")

    monkeypatch.setattr(MemoryCompactor, "_call_llm", boom)
    did = await MemoryCompactor().maybe_compact(_profile(compaction_threshold=3))
    assert did is False
    assert len(await memory_store.list("pet")) == 3  # no data loss
    assert await profile_doc_store.get("pet") is None


@pytest.mark.asyncio
async def test_compact_stores_doc_capped_at_max_doc_chars(monkeypatch):
    from app.services.memory.retriever import MAX_DOC_CHARS

    await memory_store.add("pet", "a")
    await memory_store.add("pet", "b")
    await memory_store.add("pet", "c")

    long_doc = "## User Profile\n" + "\n".join(
        f"- fact {i} with some padding text to inflate length" for i in range(200)
    )

    async def fake_call(self, profile, current_doc, facts, user_id=None):
        return long_doc

    monkeypatch.setattr(MemoryCompactor, "_call_llm", fake_call)
    did = await MemoryCompactor().maybe_compact(_profile(compaction_threshold=3))
    assert did is True
    assert len(long_doc) > MAX_DOC_CHARS  # sanity: the fake LLM reply is oversized
    doc = await profile_doc_store.get("pet")
    assert len(doc["content"]) <= MAX_DOC_CHARS


@pytest.mark.asyncio
async def test_compact_empty_doc_keeps_facts(monkeypatch):
    await memory_store.add("pet", "a")
    await memory_store.add("pet", "b")
    await memory_store.add("pet", "c")

    async def empty(self, profile, current_doc, facts, user_id=None):
        return "   "

    monkeypatch.setattr(MemoryCompactor, "_call_llm", empty)
    did = await MemoryCompactor().maybe_compact(_profile(compaction_threshold=3))
    assert did is False
    assert len(await memory_store.list("pet")) == 3


async def test_compaction_reads_and_writes_the_user_bucket(monkeypatch):
    from app.services.memory import compactor as comp
    from app.services.memory.store import memory_store, profile_doc_store
    from app.services.profiles.models import LlmConfig, MemoryConfig, Profile

    profile = Profile(
        name="template", owner_id=None,
        llm=LlmConfig(base_url="http://x", model="m"),
        memory=MemoryConfig(enabled=True, compaction_threshold=2),
    )
    await memory_store.add("template", "f1", user_id="user-a")
    await memory_store.add("template", "f2", user_id="user-a")
    await memory_store.add("template", "other", user_id="user-b")

    async def _fake_llm(self, prof, current_doc, facts, user_id=None):
        return "## User Profile\n### Sở thích\n- " + ", ".join(facts)
    monkeypatch.setattr(comp.MemoryCompactor, "_call_llm", _fake_llm)

    assert await comp.memory_compactor.maybe_compact(profile, user_id="user-a") is True
    # A's doc written under A; A's facts pruned; B untouched
    assert "f1" in (await profile_doc_store.get("template", user_id="user-a"))["content"]
    assert await profile_doc_store.get("template", user_id="user-b") is None
    assert await memory_store.list("template", user_id="user-a") == []
    assert [m["content"] for m in await memory_store.list("template", user_id="user-b")] == ["other"]
