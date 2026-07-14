import pytest

from app.services.conversation.responder import resolve_llm_override_from_registry
from app.services.model_registry.store import model_registry_store


@pytest.mark.asyncio
async def test_returns_none_when_engine_or_model_blank():
    assert await resolve_llm_override_from_registry("", "some-model") is None
    assert await resolve_llm_override_from_registry("openrouter", "") is None
    assert await resolve_llm_override_from_registry("", "") is None


@pytest.mark.asyncio
async def test_returns_none_when_no_matching_entry():
    assert await resolve_llm_override_from_registry("openrouter", "no-such-model") is None


@pytest.mark.asyncio
async def test_returns_none_when_matching_entry_has_no_key():
    await model_registry_store.create("llm", "openrouter", "keyless-model", "Keyless Model")
    assert await resolve_llm_override_from_registry("openrouter", "keyless-model") is None


@pytest.mark.asyncio
async def test_returns_base_url_and_api_key_for_a_matching_keyed_entry():
    await model_registry_store.create(
        "llm", "openrouter", "gpt-4o-mini", "GPT-4o mini",
        base_url="https://openrouter.ai/api/v1", api_key="sk-or-test",
    )
    result = await resolve_llm_override_from_registry("openrouter", "gpt-4o-mini")
    assert result == ("https://openrouter.ai/api/v1", "sk-or-test")


@pytest.mark.asyncio
async def test_does_not_match_a_different_engine_with_the_same_model_id():
    await model_registry_store.create(
        "llm", "ollama", "gpt-4o-mini", "GPT-4o mini (ollama alias)", api_key="sk-ollama-test"
    )
    assert await resolve_llm_override_from_registry("openrouter", "gpt-4o-mini") is None
