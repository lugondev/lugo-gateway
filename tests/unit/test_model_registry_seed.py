import pytest

from app.services.model_registry.seed import seed_known_models
from app.services.model_registry.store import ModelRegistryStore


@pytest.fixture
def store():
    return ModelRegistryStore()


@pytest.mark.asyncio
async def test_seed_populates_stt_and_tts_entries(store):
    await seed_known_models()
    entries = await store.list_all()
    kinds = {e["kind"] for e in entries}
    assert "stt" in kinds
    assert "tts" in kinds
    # tts entries gate at engine granularity: model_id == engine
    tts_entries = [e for e in entries if e["kind"] == "tts"]
    assert all(e["model_id"] == e["engine"] for e in tts_entries)


@pytest.mark.asyncio
async def test_seed_is_idempotent_and_preserves_admin_edits(store):
    await seed_known_models()
    entries = await store.list_all()
    stt_entry = next(e for e in entries if e["kind"] == "stt")
    await store.set_fields(stt_entry["id"], enabled=False)

    await seed_known_models()  # re-seed must not overwrite the admin's edit
    refreshed = await store.find("stt", stt_entry["engine"], stt_entry["model_id"])
    assert refreshed.enabled is False
