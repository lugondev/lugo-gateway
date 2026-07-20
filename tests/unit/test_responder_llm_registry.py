import pytest

from app.services.conversation.responder import (
    get_active_llm_api_key,
    get_active_llm_base_url,
    get_active_llm_model,
    resolve_llm_override_from_registry,
)
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


# ---------------------------------------------------------------------------
# _active_llm_entry() (exercised via get_active_llm_model/base_url/api_key) --
# the conversation responder's active LLM now resolves via is_default, not
# via "the single enabled llm row" (that invariant no longer holds; multiple
# llm rows may be enabled at once).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_active_llm_resolves_to_the_is_default_entry_among_multiple_enabled():
    await model_registry_store.create(
        "llm", "openrouter", "model-a", "Model A",
        base_url="https://a.example.com", api_key="sk-a",
    )
    await model_registry_store.create(
        "llm", "ollama", "model-b", "Model B",
        base_url="https://b.example.com", api_key="sk-b", is_default=True,
    )

    assert await get_active_llm_model() == "model-b"
    assert await get_active_llm_base_url() == "https://b.example.com"
    assert await get_active_llm_api_key() == "sk-b"


@pytest.mark.asyncio
async def test_active_llm_falls_back_to_nothing_when_the_default_is_disabled():
    """A row marked is_default but currently disabled must not be used --
    fail closed to "no active LLM" rather than silently serving a disabled
    model."""
    await model_registry_store.create(
        "llm", "ollama", "model-b", "Model B",
        base_url="https://b.example.com", api_key="sk-b",
        is_default=True, enabled=False,
    )

    assert await get_active_llm_model() == ""
    assert await get_active_llm_base_url() == ""
    assert await get_active_llm_api_key() == ""
