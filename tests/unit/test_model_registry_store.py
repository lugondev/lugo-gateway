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
    assert entry["api_key"] == ""
    assert entry["base_url"] == ""


@pytest.mark.asyncio
async def test_create_persists_api_key_and_base_url(store):
    entry = await store.create(
        "llm", "openrouter", "qwen3-asr-flash", "Qwen3 ASR Flash (OpenRouter)",
        api_key="sk-or-secret", base_url="https://openrouter.ai/api/v1",
    )
    assert entry["api_key"] == "sk-or-secret"
    assert entry["base_url"] == "https://openrouter.ai/api/v1"

    found = await store.find("llm", "openrouter", "qwen3-asr-flash")
    assert found["api_key"] == "sk-or-secret"
    assert found["base_url"] == "https://openrouter.ai/api/v1"


@pytest.mark.asyncio
async def test_has_key_for_engine_true_only_when_an_enabled_entry_has_a_key(store):
    assert await store.has_key_for_engine("stt", "qwen3_asr_or") is False

    await store.create("stt", "qwen3_asr_or", "qwen/qwen3-asr-flash-2026-02-10", "Qwen3 ASR Flash")
    assert await store.has_key_for_engine("stt", "qwen3_asr_or") is False  # no key yet

    entry = await store.create(
        "stt", "qwen3_asr_or", "qwen/other-model", "Other model", api_key="sk-or-test"
    )
    assert await store.has_key_for_engine("stt", "qwen3_asr_or") is True

    await store.set_fields(entry["id"], enabled=False)
    assert await store.has_key_for_engine("stt", "qwen3_asr_or") is False  # disabled entries don't count


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
async def test_set_fields_updates_api_key_and_base_url(store):
    created = await store.create("llm", "openrouter", "qwen3-asr-flash", "Qwen3 ASR Flash (OpenRouter)")
    updated = await store.set_fields(
        created["id"], api_key="sk-or-new-key", base_url="https://openrouter.ai/api/v1"
    )
    assert updated["api_key"] == "sk-or-new-key"
    assert updated["base_url"] == "https://openrouter.ai/api/v1"


@pytest.mark.asyncio
async def test_list_all_returns_every_entry(store):
    await store.create("stt", "whisper", "medium", "Whisper Medium")
    await store.create("tts", "omnivoice", "omnivoice", "OmniVoice")
    entries = await store.list_all()
    assert len(entries) == 2


@pytest.mark.asyncio
async def test_reads_are_served_from_cache_without_hitting_the_db_again(store, monkeypatch):
    """The whole point of caching: find()/list_all()/has_key_for_engine() must
    not re-query the DB once the cache is warm (create()'s own DB write is the
    only DB access that should happen here)."""
    await store.create("stt", "whisper", "medium", "Whisper Medium", api_key="sk-test")

    def _boom(*a, **kw):
        raise AssertionError("db_session() called again after the cache was already warm")

    monkeypatch.setattr("app.services.model_registry.store.db_session", _boom)

    assert await store.find("stt", "whisper", "medium") is not None
    assert len(await store.list_all()) == 1
    assert await store.has_key_for_engine("stt", "whisper") is True
    assert await store.get((await store.list_all())[0]["id"]) is not None


@pytest.mark.asyncio
async def test_invalidate_forces_a_fresh_load_on_next_access(store):
    await store.create("stt", "whisper", "medium", "Whisper Medium")
    assert len(await store.list_all()) == 1

    store.invalidate()
    assert store._by_id is None  # confirms invalidate() actually cleared the cache
    assert len(await store.list_all()) == 1  # reloads transparently, same data


@pytest.mark.asyncio
async def test_set_fields_update_is_visible_via_find_without_a_reload(store, monkeypatch):
    """set_fields() must update self._by_id in place -- a subsequent find()
    should see the change purely from cache, not by re-querying the DB (the
    only DB access after this point should be set_fields' own required write,
    which already happened by the time we monkeypatch)."""
    created = await store.create("stt", "whisper", "medium", "Whisper Medium")
    await store.set_fields(created["id"], api_key="sk-updated")

    def _boom(*a, **kw):
        raise AssertionError("db_session() called for a read after set_fields already updated the cache")

    monkeypatch.setattr("app.services.model_registry.store.db_session", _boom)
    found = await store.find("stt", "whisper", "medium")
    assert found["api_key"] == "sk-updated"


@pytest.mark.asyncio
async def test_mutating_returned_entries_does_not_corrupt_the_cache(store):
    """Routes mask api_key on the dicts the store hands back (see
    routes/model_registry.py). If those are the cached objects themselves,
    the mask overwrites the real key in the cache and every hot-path find()
    (responder/openrouter_provider) reads the masked garbage until restart."""
    created = await store.create(
        "llm", "openrouter", "some-model", "Some Model", api_key="sk-or-v1-realkey"
    )

    created["api_key"] = "MASKED"
    (await store.list_all())[0]["api_key"] = "MASKED"
    (await store.get(created["id"]))["api_key"] = "MASKED"
    (await store.find("llm", "openrouter", "some-model"))["api_key"] = "MASKED"
    (await store.find_enabled("llm"))["api_key"] = "MASKED"

    found = await store.find("llm", "openrouter", "some-model")
    assert found["api_key"] == "sk-or-v1-realkey"


@pytest.mark.asyncio
async def test_mutating_set_fields_result_does_not_corrupt_the_cache(store):
    created = await store.create("llm", "openrouter", "some-model", "Some Model")
    updated = await store.set_fields(created["id"], api_key="sk-or-v1-realkey")
    updated["api_key"] = "MASKED"

    found = await store.find("llm", "openrouter", "some-model")
    assert found["api_key"] == "sk-or-v1-realkey"


@pytest.mark.asyncio
async def test_mutating_returned_nested_config_does_not_corrupt_the_cache(store):
    await store.create("stt", "qwen3_asr", "", "Qwen3", config={"device": "mps"})
    (await store.find("stt", "qwen3_asr", ""))["config"]["device"] = "corrupted"
    store.find_sync("stt", "qwen3_asr", "")["config"]["device"] = "corrupted"

    assert (await store.find("stt", "qwen3_asr", ""))["config"] == {"device": "mps"}


def test_find_sync_returns_none_before_cache_warmed():
    store = ModelRegistryStore()
    assert store.find_sync("stt", "whisper_local", "") is None
    assert store.find_enabled_sync("stt", "whisper_local") is None


@pytest.mark.asyncio
async def test_find_sync_reads_the_warmed_cache():
    store = ModelRegistryStore()
    created = await store.create("stt", "whisper_local", "", "Whisper Local", config={"device": "cuda"})
    assert store.find_sync("stt", "whisper_local", "") == created
    assert store.find_enabled_sync("stt", "whisper_local") == created
    assert store.find_enabled_sync("stt", "qwen3_asr") is None


@pytest.mark.asyncio
async def test_find_enabled_sync_skips_disabled_entries():
    store = ModelRegistryStore()
    entry = await store.create("tts", "omnivoice", "k2-fsa/OmniVoice", "OmniVoice")
    await store.set_fields(entry["id"], enabled=False)
    assert store.find_enabled_sync("tts", "omnivoice") is None
