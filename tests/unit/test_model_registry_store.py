import pytest

from app.services.model_registry.store import ModelRegistryStore


@pytest.fixture
def store():
    return ModelRegistryStore()


@pytest.mark.asyncio
async def test_create_defaults_enabled_true_stable(store):
    entry = await store.create("stt", "whisper", "medium", "Whisper Medium")
    assert entry["enabled"] is True
    assert entry["stage"] == "stable"


@pytest.mark.asyncio
async def test_find_matches_exact_triple(store):
    await store.create("stt", "whisper", "medium", "Whisper Medium")
    found = await store.find("stt", "whisper", "medium")
    assert found is not None
    assert await store.find("stt", "whisper", "large") is None


@pytest.mark.asyncio
async def test_set_fields_updates_enabled_and_stage(store):
    created = await store.create("llm", "openrouter", "qwen3-asr-flash", "Qwen3 ASR Flash (OpenRouter)")
    updated = await store.set_fields(created["id"], enabled=False, stage="testing")
    assert updated["enabled"] is False
    assert updated["stage"] == "testing"
    assert await store.set_fields("missing-id", enabled=False) is None


@pytest.mark.asyncio
async def test_list_all_returns_every_entry(store):
    await store.create("stt", "whisper", "medium", "Whisper Medium")
    await store.create("tts", "omnivoice", "omnivoice", "OmniVoice")
    entries = await store.list_all()
    assert len(entries) == 2
