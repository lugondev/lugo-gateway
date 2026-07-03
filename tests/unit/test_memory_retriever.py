import pytest

from app.services.memory.embedder import cosine
from app.services.memory.retriever import MemoryRetriever, inject_memories
from app.services.memory.store import memory_store
from app.services.profiles.models import Profile


def test_cosine():
    assert cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine([0, 0], [1, 1]) == 0.0  # zero vector guard


def test_inject_memories():
    assert inject_memories("base", "") == "base"
    out = inject_memories("base", "## User Memories\n- x")
    assert out.startswith("## User Memories\n- x")
    assert out.endswith("base")
    assert inject_memories("", "## User Memories\n- x") == "## User Memories\n- x"


@pytest.mark.asyncio
async def test_get_context_all_mode():
    await memory_store.add("pet", "likes tea")
    await memory_store.add("pet", "from Hanoi")
    profile = Profile(name="pet")
    block = await MemoryRetriever().get_context(profile)
    assert block.startswith("## User Memories")
    assert "- likes tea" in block and "- from Hanoi" in block


@pytest.mark.asyncio
async def test_get_context_empty_cases():
    r = MemoryRetriever()
    assert await r.get_context(None) == ""
    assert await r.get_context(Profile(name="empty")) == ""
    disabled = Profile(name="pet", memory={"enabled": False})
    await memory_store.add("pet", "x")
    assert await r.get_context(disabled) == ""


@pytest.mark.asyncio
async def test_semantic_mode_top_k(monkeypatch):
    await memory_store.add("pet", "likes tea", embedding=[1.0, 0.0])
    await memory_store.add("pet", "plays guitar", embedding=[0.0, 1.0])

    async def fake_embed(texts, base_url, api_key, model):
        return [[1.0, 0.0]]  # query vector ~ "tea"

    monkeypatch.setattr("app.services.memory.retriever.embed_texts", fake_embed)
    profile = Profile(
        name="pet",
        llm={"base_url": "http://llm.local/v1"},
        memory={"mode": "semantic", "top_k": 1, "embed_model": "emb"},
    )
    block = await MemoryRetriever().get_context(profile, query="tea?")
    assert "- likes tea" in block
    assert "guitar" not in block


@pytest.mark.asyncio
async def test_semantic_falls_back_when_no_embeddings(caplog):
    await memory_store.add("pet", "no vector here")
    profile = Profile(
        name="pet",
        llm={"base_url": "http://llm.local/v1"},
        memory={"mode": "semantic", "embed_model": "emb"},
    )
    with caplog.at_level("WARNING"):
        block = await MemoryRetriever().get_context(profile, query="anything")
    assert "- no vector here" in block
    assert any("falling back to all" in r.message for r in caplog.records)
