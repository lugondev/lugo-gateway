"""The search_knowledge tool: rendering, budget, fail-open, metering."""

from __future__ import annotations

from app.services.conversation.tools.base import ToolContext
from app.services.conversation.tools.knowledge import (
    MAX_CHARS,
    NO_HITS,
    KnowledgeToolSource,
)
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


async def test_an_oversized_first_hit_does_not_hide_the_rest():
    """The proven case. `_render` used to `break` on the first hit that did not
    fit, so a single long chunk left `parts` empty, `_render` returned "", and
    `_run` fell through to NO_HITS -- the tool reported "no matching documents"
    while holding a perfectly usable second one. `break` also discarded every
    later hit."""
    hits = [_hit(text="x" * 2500, heading="Dài"), _hit(text="Mười hai tháng.", heading="Bảo hành")]
    src = KnowledgeToolSource(_profile(), FakeClient(hits=hits, tokens=4), user_id="u")
    out = await src.list_tools()[0].run({"query": "q"}, _ctx())
    assert out != NO_HITS
    assert "Mười hai tháng." in out
    assert len(out) <= MAX_CHARS


async def test_an_oversized_hit_is_truncated_at_a_line_boundary_never_mid_line():
    """Spec, *Result shape*: truncated at a line boundary, never mid-line."""
    lines = [f"dòng số {i:03d} " + "y" * 60 for i in range(60)]
    src = KnowledgeToolSource(_profile(), FakeClient(hits=[_hit(text="\n".join(lines))]), user_id="u")
    out = await src.list_tools()[0].run({"query": "q"}, _ctx())
    assert len(out) <= MAX_CHARS
    body = [ln for ln in out.splitlines() if ln and not ln.startswith("###")]
    assert body, "the hit was dropped entirely instead of truncated"
    # Every rendered line is a whole source line: nothing was cut mid-line.
    assert all(ln in lines for ln in body)
    assert len(body) < len(lines), "nothing was actually truncated -- test is not exercising it"


async def test_hits_after_an_oversized_one_still_render():
    hits = [
        _hit(text="a" * 1200, heading="H0"),
        _hit(text="b" * 1500, heading="H1"),   # cannot fit in what is left
        _hit(text="ngắn gọn", heading="H2"),   # but this still can
    ]
    src = KnowledgeToolSource(_profile(), FakeClient(hits=hits), user_id="u")
    out = await src.list_tools()[0].run({"query": "q"}, _ctx())
    assert "ngắn gọn" in out
    assert len(out) <= MAX_CHARS


async def test_hits_that_cannot_be_rendered_are_not_reported_as_no_hits():
    """A single unbreakable oversized chunk: honest about what happened rather
    than claiming the knowledge base holds nothing."""
    src = KnowledgeToolSource(_profile(), FakeClient(hits=[_hit(text="z" * 5000)]), user_id="u")
    out = await src.list_tools()[0].run({"query": "q"}, _ctx())
    assert out.strip()
    assert out != NO_HITS


async def test_a_non_string_query_never_escapes_as_an_exception():
    """The spec mandates never-an-exception for this tool. `query.strip()` sat
    outside the try, so {"query": 123} reached ToolRegistry.run's generic
    handler as "'int' object has no attribute 'strip'"."""
    src = KnowledgeToolSource(_profile(), FakeClient(hits=[_hit()]), user_id="u")
    out = await src.list_tools()[0].run({"query": 123}, _ctx())
    assert out.strip()


async def test_a_non_object_arguments_blob_never_escapes_as_an_exception():
    src = KnowledgeToolSource(_profile(), FakeClient(hits=[_hit()]), user_id="u")
    out = await src.list_tools()[0].run("not an object", _ctx())
    assert out.strip()


async def test_a_structured_query_value_is_refused_not_stringified():
    src = KnowledgeToolSource(_profile(), FakeClient(hits=[_hit()]), user_id="u")
    client = src._client
    out = await src.list_tools()[0].run({"query": {"text": "q"}}, _ctx())
    assert out.strip()
    assert client.calls == []
