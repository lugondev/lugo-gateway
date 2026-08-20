"""A knowledge search spends embedding money, so it records a row."""

from __future__ import annotations

from app.services.conversation.tools.base import ToolContext
from app.services.conversation.tools.knowledge import KnowledgeToolSource
from app.services.knowledge.client import KnowledgeUnavailable
from app.services.profiles.models import KnowledgeConfig, LlmConfig, Profile


class FakeClient:
    def __init__(self, tokens=0, error=None):
        self._tokens, self._error = tokens, error

    async def search_with_usage(self, collection, query, *, limit, min_score):
        if self._error:
            raise self._error
        return [], self._tokens


def _profile():
    return Profile(
        name="shop",
        knowledge=KnowledgeConfig(
            enabled=True, collection="faq", embed_model="text-embedding-3-small"
        ),
    )


async def test_a_successful_search_records_one_embed_row(monkeypatch):
    rows = []

    async def fake_record(**kw):
        rows.append(kw)

    monkeypatch.setattr("app.services.usage.recorder.record_usage", fake_record)
    src = KnowledgeToolSource(_profile(), FakeClient(tokens=11), user_id="u1")
    await src.list_tools()[0].run({"query": "q"}, ToolContext())

    assert len(rows) == 1
    assert rows[0]["kind"] == "embed"
    assert rows[0]["prompt_tokens"] == 11
    assert rows[0]["user_id"] == "u1"
    assert rows[0]["profile_id"] == "shop"
    # Blank would silently price at $0 forever -- see recorder.record_usage.
    assert rows[0]["model_id"] == "text-embedding-3-small"


async def test_a_failed_search_records_nothing(monkeypatch):
    rows = []

    async def fake_record(**kw):
        rows.append(kw)

    monkeypatch.setattr("app.services.usage.recorder.record_usage", fake_record)
    src = KnowledgeToolSource(
        _profile(), FakeClient(error=KnowledgeUnavailable("down")), user_id="u1"
    )
    await src.list_tools()[0].run({"query": "q"}, ToolContext())

    assert rows == []


async def test_the_row_never_claims_the_profiles_llm_engine(monkeypatch):
    """kbase embeds with its OWN provider, under its own KB_EMBED_MODEL. The
    gateway's LLM engine has no relationship to it, and stamping it on the row
    poisons the (kind, engine, model_id) registry key: resolve_usage_model
    returns both unchanged when both are non-blank, so the lookup misses,
    provider_id is "" and the cost is $0 -- the exact failure the spec's
    Metering section warns about, arriving through `engine` instead of
    `model_id`. (The memory precedent this copied is not analogous: memory
    embeds through profile.llm.base_url, so there the engine really did spend
    the money.)"""
    rows = []

    async def fake_record(**kw):
        rows.append(kw)

    monkeypatch.setattr("app.services.usage.recorder.record_usage", fake_record)
    profile = Profile(
        name="shop",
        llm=LlmConfig(engine="openrouter", model="qwen-max"),
        knowledge=KnowledgeConfig(
            enabled=True, collection="faq", embed_model="text-embedding-3-small"
        ),
    )
    src = KnowledgeToolSource(profile, FakeClient(tokens=7), user_id="u1")
    await src.list_tools()[0].run({"query": "q"}, ToolContext())

    assert rows[0]["engine"] != "openrouter"
    # Blank, deliberately: attribution resolves the engine from the declared
    # embed model, which is the only thing the gateway actually knows.
    assert rows[0]["engine"] == ""


async def test_a_search_lands_a_priced_row_with_a_resolvable_engine():
    """End to end through the real recorder: the row must reach the registry
    entry that carries the price, not sit at $0 with a blank provider."""
    from sqlalchemy import select

    from app.services.db.engine import db_session, init_db
    from app.services.db.models import UsageEvent
    from app.services.model_registry.store import model_registry_store

    await init_db()
    await model_registry_store.create(
        "embed", "openai_embed", "text-embedding-3-small", "OpenAI small",
        config={"provider_id": "prov-embed", "price": {"unit": "1M_tokens", "in": 0.02}},
    )
    profile = Profile(
        name="shop",
        llm=LlmConfig(engine="openrouter", model="qwen-max"),
        knowledge=KnowledgeConfig(
            enabled=True, collection="faq", embed_model="text-embedding-3-small"
        ),
    )
    src = KnowledgeToolSource(profile, FakeClient(tokens=1000), user_id="u1")
    await src.list_tools()[0].run({"query": "q"}, ToolContext())

    async with db_session() as s:
        row = (await s.execute(select(UsageEvent).where(UsageEvent.kind == "embed"))).scalars().one()
    assert row.engine == "openai_embed"
    assert row.provider_id == "prov-embed"
    assert row.cost_usd > 0
