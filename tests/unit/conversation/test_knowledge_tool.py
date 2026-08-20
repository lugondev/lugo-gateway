"""The search_knowledge tool: rendering, budget, fail-open, metering."""

from __future__ import annotations

from app.services.conversation.tools.base import ToolContext
from app.services.conversation.tools.knowledge import MAX_CHARS, KnowledgeToolSource
from app.services.knowledge.client import KnowledgeUnavailable
from app.services.profiles.models import KnowledgeConfig, Profile


class FakeClient:
    def __init__(self, hits=None, tokens=0, error=None):
        self._hits, self._tokens, self._error = hits or [], tokens, error
        self.calls = []

    async def search_with_usage(self, collection, query, *, limit, min_score):
        self.calls.append((collection, query, limit, min_score))
        if self._error:
            raise self._error
        return self._hits, self._tokens


def _profile(**kw):
    cfg = {"enabled": True, "collection": "faq", "embed_model": "m", **kw}
    return Profile(name="shop", knowledge=KnowledgeConfig(**cfg))


def _hit(text="Mười hai tháng.", heading="Bảo hành", title="Sổ tay"):
    return {"text": text, "title": title, "filename": "s.md", "heading": heading, "score": 0.9}


def _ctx():
    return ToolContext()


async def test_the_operator_description_reaches_the_schema_verbatim():
    desc = "Tra cứu sổ tay bảo hành và chính sách đổi trả"
    src = KnowledgeToolSource(_profile(description=desc), FakeClient(), user_id="u")
    tool = src.list_tools()[0]
    assert tool.name == "search_knowledge"
    assert tool.description == desc


async def test_a_blank_description_falls_back_without_being_empty():
    src = KnowledgeToolSource(_profile(description=""), FakeClient(), user_id="u")
    tool = src.list_tools()[0]
    assert tool.description.strip()
    assert "faq" in tool.description


async def test_the_model_may_only_pass_a_query():
    src = KnowledgeToolSource(_profile(), FakeClient(), user_id="u")
    params = src.list_tools()[0].parameters
    assert set(params["properties"]) == {"query"}
    assert params["required"] == ["query"]


async def test_a_hit_is_rendered_with_its_heading_path():
    client = FakeClient(hits=[_hit()], tokens=4)
    src = KnowledgeToolSource(_profile(), client, user_id="u")
    out = await src.list_tools()[0].run({"query": "bảo hành"}, _ctx())
    assert "Sổ tay > Bảo hành" in out
    assert "Mười hai tháng." in out


async def test_profile_settings_drive_the_search_not_the_model():
    client = FakeClient(hits=[])
    src = KnowledgeToolSource(_profile(top_k=3, min_score=0.7), client, user_id="u")
    await src.list_tools()[0].run({"query": "q", "limit": 99, "collection": "other"}, _ctx())
    assert client.calls == [("faq", "q", 3, 0.7)]


async def test_no_hits_says_so_rather_than_returning_nothing():
    src = KnowledgeToolSource(_profile(), FakeClient(hits=[]), user_id="u")
    out = await src.list_tools()[0].run({"query": "q"}, _ctx())
    assert out.strip()


async def test_the_rendered_block_respects_the_budget():
    hits = [_hit(text="x" * 500, heading=f"H{i}") for i in range(20)]
    src = KnowledgeToolSource(_profile(), FakeClient(hits=hits), user_id="u")
    out = await src.list_tools()[0].run({"query": "q"}, _ctx())
    assert len(out) <= MAX_CHARS


async def test_a_failure_never_leaks_the_url_or_the_driver_error():
    err = KnowledgeUnavailable("knowledge search failed: connect to http://kb.internal:8090 refused")
    src = KnowledgeToolSource(_profile(), FakeClient(error=err), user_id="u")
    out = await src.list_tools()[0].run({"query": "q"}, _ctx())
    assert "kb.internal" not in out
    assert "8090" not in out
    assert out.strip()


async def test_an_unexpected_error_also_fails_open():
    src = KnowledgeToolSource(_profile(), FakeClient(error=RuntimeError("boom")), user_id="u")
    out = await src.list_tools()[0].run({"query": "q"}, _ctx())
    assert "boom" not in out
    assert out.strip()
