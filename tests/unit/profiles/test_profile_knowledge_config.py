"""The knowledge block is off by default and optional on stored rows."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.services.profiles.models import KnowledgeConfig, Profile
from app.services.system_config import SystemConfig


def test_a_profile_has_knowledge_disabled_by_default():
    p = Profile(name="p")
    assert p.knowledge.enabled is False
    assert p.knowledge.collection == ""
    assert p.knowledge.top_k == 5
    assert p.knowledge.min_score == 0.35


def test_a_stored_profile_without_a_knowledge_block_still_loads():
    # Every profile already persisted predates this field. If the model
    # required it, the first read after deploy would fail for all of them.
    p = Profile.model_validate({"name": "legacy"})
    assert p.knowledge.enabled is False


def test_a_knowledge_block_round_trips_through_serialization():
    p = Profile.model_validate(
        {
            "name": "shop",
            "knowledge": {
                "enabled": True,
                "collection": "faq",
                "description": "Tra cứu sổ tay bảo hành",
                "top_k": 3,
                "min_score": 0.5,
                "embed_model": "text-embedding-3-small",
            },
        }
    )
    again = Profile.model_validate(p.model_dump())
    assert again.knowledge.collection == "faq"
    assert again.knowledge.description == "Tra cứu sổ tay bảo hành"
    assert again.knowledge.top_k == 3
    assert again.knowledge.embed_model == "text-embedding-3-small"


def test_the_service_block_defaults_to_unconfigured():
    cfg = SystemConfig()
    assert cfg.knowledge.base_url == ""
    assert cfg.knowledge.api_key == ""
    assert cfg.knowledge.timeout_seconds == 10.0


@pytest.mark.parametrize(
    "field,value",
    [("top_k", 0), ("top_k", -1), ("top_k", 51), ("min_score", -0.1), ("min_score", 1.1)],
)
def test_out_of_range_search_settings_are_rejected(field, value):
    """kbase's SearchRequest pins limit to 1..50 and min_score to 0.0..1.0
    (servers/knowledge-api/src/kbase/types.py). Unbounded here means an
    operator can store a value that 422s on every single call -- fail-open, so
    the tool just never returns anything and never says why. Rejecting at the
    model turns that into a 422 on the one PUT that caused it."""
    with pytest.raises(ValidationError):
        KnowledgeConfig(**{field: value})


@pytest.mark.parametrize(
    "field,value",
    [("top_k", 1), ("top_k", 50), ("min_score", 0.0), ("min_score", 1.0)],
)
def test_the_bounds_match_what_kbase_itself_accepts(field, value):
    assert getattr(KnowledgeConfig(**{field: value}), field) == value
