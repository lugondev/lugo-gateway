import pytest

from app.services.db.engine import init_db
from app.services.providers.store import provider_store


@pytest.mark.asyncio
async def test_create_get_and_sync_readback():
    await init_db()
    created = await provider_store.create(
        name="openrouter", label="OpenRouter",
        base_url="https://openrouter.ai/api/v1", api_key="sk-or-x",
    )
    pid = created["id"]
    assert created["name"] == "openrouter"

    got = await provider_store.get(pid)
    assert got["api_key"] == "sk-or-x"

    # sync path (used off the event loop) sees the same cached row
    assert provider_store.get_sync(pid)["base_url"] == "https://openrouter.ai/api/v1"


@pytest.mark.asyncio
async def test_set_fields_and_delete():
    await init_db()
    created = await provider_store.create(name="openai", base_url="x", api_key="k")
    pid = created["id"]

    updated = await provider_store.set_fields(pid, api_key="k2", enabled=False)
    assert updated["api_key"] == "k2"
    assert updated["enabled"] is False

    assert await provider_store.delete(pid) is True
    assert await provider_store.get(pid) is None
    assert provider_store.get_sync(pid) is None


@pytest.mark.asyncio
async def test_get_sync_returns_none_when_cache_cold():
    provider_store.invalidate()
    assert provider_store.get_sync("anything") is None
