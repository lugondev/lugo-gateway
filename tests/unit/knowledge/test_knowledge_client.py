"""The client against a stubbed kbase."""

from __future__ import annotations

import httpx
import pytest

from app.services.knowledge.client import KnowledgeClient, KnowledgeUnavailable

BODY = {
    "chunks": [
        {
            "text": "Bảo hành mười hai tháng.",
            "document_id": "d1",
            "title": "Sổ tay",
            "filename": "sotay.md",
            "heading": "Bảo hành",
            "score": 0.91,
        }
    ],
    "usage": {"prompt_tokens": 7},
}


def _client(handler, **kw):
    transport = httpx.MockTransport(handler)
    return KnowledgeClient(
        base_url="http://kb.invalid", api_key="secret-key", timeout=1.0, transport=transport, **kw
    )


async def test_it_parses_hits_and_usage():
    async def handler(request):
        return httpx.Response(200, json=BODY)

    hits, tokens = await _client(handler).search_with_usage("faq", "bảo hành", limit=5, min_score=0.3)

    assert tokens == 7
    assert hits[0]["text"] == "Bảo hành mười hai tháng."
    assert hits[0]["heading"] == "Bảo hành"


async def test_it_sends_the_bearer_credential_and_the_query():
    seen = {}

    async def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.read().decode()
        return httpx.Response(200, json=BODY)

    await _client(handler).search_with_usage("faq", "bảo hành", limit=3, min_score=0.4)

    assert seen["auth"] == "Bearer secret-key"
    assert '"collection": "faq"' in seen["body"] or '"collection":"faq"' in seen["body"]


async def test_a_non_200_raises_knowledge_unavailable():
    async def handler(request):
        return httpx.Response(503, text="upstream down")

    with pytest.raises(KnowledgeUnavailable):
        await _client(handler).search_with_usage("faq", "q", limit=5, min_score=0.3)


async def test_a_transport_error_raises_knowledge_unavailable():
    async def handler(request):
        raise httpx.ConnectError("no route to host")

    with pytest.raises(KnowledgeUnavailable):
        await _client(handler).search_with_usage("faq", "q", limit=5, min_score=0.3)


async def test_a_malformed_body_raises_knowledge_unavailable():
    async def handler(request):
        return httpx.Response(200, text="not json")

    with pytest.raises(KnowledgeUnavailable):
        await _client(handler).search_with_usage("faq", "q", limit=5, min_score=0.3)


async def test_missing_usage_counts_as_zero_tokens():
    async def handler(request):
        return httpx.Response(200, json={"chunks": []})

    hits, tokens = await _client(handler).search_with_usage("faq", "q", limit=5, min_score=0.3)
    assert hits == []
    assert tokens == 0
