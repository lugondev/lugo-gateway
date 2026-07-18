import pytest

from app.services.model_registry import seed as seed_module
from app.services.model_registry.store import ModelRegistryStore


def test_seed_known_models_is_gone():
    """The registry no longer mirrors the code-owned catalogue. gate.py treats a
    missing entry as unrestricted, so seeding a permissive row per known model
    restricted nothing -- it only manufactured shadow rows. The catalogue lives
    in whisper_models.py; the registry holds config + restriction."""
    assert not hasattr(seed_module, "seed_known_models")


@pytest.mark.asyncio
async def test_fresh_store_has_no_catalogue_rows():
    """A store nobody has seeded is empty -- no per-variant STT rows, no
    model_id==engine TTS placeholder rows. (Migrations add the model_id=""
    sentinels; those are exercised in the migration tests, not here.)"""
    store = ModelRegistryStore()
    assert await store.list_all() == []
