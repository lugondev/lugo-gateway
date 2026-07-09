import pytest

from app.core.settings import settings
from app.services.conversation.responder import (
    VOICE_OPTIMIZATION_DIRECTIVE,
    resolve_system_prompt,
)
from app.services.system_config import SystemConfigStore


@pytest.fixture(autouse=True)
def _fresh_store(tmp_path, monkeypatch):
    store = SystemConfigStore(str(tmp_path / "system_config.json"))
    monkeypatch.setattr("app.services.conversation.responder.system_config_store", store)
    return store


def test_no_base_context_returns_persona_prompt_unchanged(_fresh_store):
    assert resolve_system_prompt("You are a pirate.") == "You are a pirate."


def test_no_base_context_falls_back_to_settings_default():
    assert resolve_system_prompt(None) == settings.conversation_system_prompt


def test_base_context_prepended_to_explicit_persona(_fresh_store):
    _fresh_store.set_base_context("Platform: TeguVoice. Never give medical advice.")
    result = resolve_system_prompt("You are a pirate.")
    assert result == "Platform: TeguVoice. Never give medical advice.\n\nYou are a pirate."


def test_base_context_prepended_to_default_persona(_fresh_store):
    _fresh_store.set_base_context("Platform: TeguVoice.")
    result = resolve_system_prompt(None)
    assert result == f"Platform: TeguVoice.\n\n{settings.conversation_system_prompt}"


def test_voice_optimized_off_by_default(_fresh_store):
    result = resolve_system_prompt("You are a pirate.")
    assert VOICE_OPTIMIZATION_DIRECTIVE not in result


def test_voice_optimized_appends_directive_at_end(_fresh_store):
    result = resolve_system_prompt("You are a pirate.", voice_optimized=True)
    assert result == f"You are a pirate.\n\n{VOICE_OPTIMIZATION_DIRECTIVE}"
    assert result.endswith(VOICE_OPTIMIZATION_DIRECTIVE)


def test_voice_optimized_stays_last_after_base_context(_fresh_store):
    _fresh_store.set_base_context("Platform: TeguVoice.")
    result = resolve_system_prompt("You are a pirate.", voice_optimized=True)
    assert result == (
        f"Platform: TeguVoice.\n\nYou are a pirate.\n\n{VOICE_OPTIMIZATION_DIRECTIVE}"
    )
