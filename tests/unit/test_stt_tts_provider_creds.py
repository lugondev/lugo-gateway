import pytest

from app.services.db.engine import init_db
from app.services.model_registry.store import model_registry_store
from app.services.providers.store import provider_store
from app.services.providers.resolve import resolve_credentials_sync
from app.services.stt.providers.openrouter_provider import OpenRouterSttProvider


@pytest.mark.asyncio
async def test_sync_resolver_used_by_providers():
    await init_db()
    # warm the sync cache
    p = await provider_store.create(name="openai", base_url="https://api.openai.com/v1", api_key="sk-S")
    entry = {"base_url": "", "api_key": "", "config": {"provider_id": p["id"]}}
    assert resolve_credentials_sync(entry) == ("https://api.openai.com/v1", "sk-S")


@pytest.mark.asyncio
async def test_openrouter_stt_provider_resolves_linked_provider_api_key():
    """A registry entry with a blank own api_key but a linked provider_id must
    resolve to the linked provider's api_key via the async resolver (which
    warms the registry lookup) -- not the sync/cache-only variant."""
    await init_db()
    provider = await provider_store.create(
        name="openrouter", base_url="https://openrouter.ai/api/v1", api_key="sk-or-linked"
    )
    model_id = "qwen/qwen3-asr-flash-2026-02-10"
    await model_registry_store.create(
        "stt",
        "qwen3_asr_or",
        model_id,
        "Qwen3 ASR Flash",
        api_key="",
        config={"provider_id": provider["id"]},
    )

    provider_obj = OpenRouterSttProvider(name="qwen3_asr_or", model=model_id)
    resolved_key = await provider_obj._resolve_api_key(model_id)

    assert resolved_key == "sk-or-linked"
