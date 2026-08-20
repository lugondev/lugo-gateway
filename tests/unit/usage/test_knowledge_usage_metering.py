"""A knowledge search spends embedding money, so it records a row."""

from __future__ import annotations

from app.services.conversation.tools.base import ToolContext
from app.services.conversation.tools.knowledge import KnowledgeToolSource
from app.services.knowledge.client import KnowledgeUnavailable
from app.services.profiles.models import KnowledgeConfig, Profile


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
