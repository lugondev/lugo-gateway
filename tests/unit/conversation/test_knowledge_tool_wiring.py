"""The tool appears only when the service and the profile both say so."""

from __future__ import annotations

import pytest

from app.services.conversation.session import _build_tool_registry
from app.services.profiles.models import KnowledgeConfig, Profile


@pytest.fixture
def configured(monkeypatch):
    from app.services import system_config as sc

    cfg = sc.system_config_store.get().model_copy(deep=True)
    cfg.knowledge.base_url = "http://kb.invalid"
    cfg.knowledge.api_key = "k"
    monkeypatch.setattr(sc.system_config_store, "get", lambda: cfg)
    return cfg


def _profile(**kw):
    return Profile(name="shop", knowledge=KnowledgeConfig(**kw))


async def _names(profile):
    reg = await _build_tool_registry(profile)
    return reg.names() if reg else []


async def test_the_tool_is_present_when_configured_and_enabled(configured):
    assert "search_knowledge" in await _names(_profile(enabled=True, collection="faq"))


async def test_absent_when_the_profile_has_not_enabled_it(configured):
    assert "search_knowledge" not in await _names(_profile(enabled=False, collection="faq"))


async def test_absent_when_no_collection_is_bound(configured):
    assert "search_knowledge" not in await _names(_profile(enabled=True, collection=""))


async def test_absent_when_the_service_is_not_configured(monkeypatch):
    from app.services import system_config as sc

    cfg = sc.system_config_store.get().model_copy(deep=True)
    cfg.knowledge.base_url = ""
    monkeypatch.setattr(sc.system_config_store, "get", lambda: cfg)
    assert "search_knowledge" not in await _names(_profile(enabled=True, collection="faq"))


async def test_a_profile_with_no_knowledge_block_is_unaffected(configured):
    assert "search_knowledge" not in await _names(Profile(name="plain"))
