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


async def test_absent_when_the_service_has_no_credential(monkeypatch):
    """kbase's auth middleware rejects every request without a valid bearer key
    (servers/knowledge-api/src/kbase/server/auth.py). A base_url with a blank
    api_key therefore registers a tool that 401s on EVERY call: fail-open means
    the assistant answers anyway, so the only operator-visible symptom is that
    it never cites anything -- silently, forever. Not registering it is the
    same "no tool rather than a tool that fails on every call" rule the other
    three switches already follow."""
    from app.services import system_config as sc

    cfg = sc.system_config_store.get().model_copy(deep=True)
    cfg.knowledge.base_url = "http://kb.invalid"
    cfg.knowledge.api_key = ""
    monkeypatch.setattr(sc.system_config_store, "get", lambda: cfg)
    assert "search_knowledge" not in await _names(_profile(enabled=True, collection="faq"))
