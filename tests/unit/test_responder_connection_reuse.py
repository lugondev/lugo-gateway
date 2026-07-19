"""OpenAICompatResponder must reuse one httpx.AsyncClient across calls instead
of opening a fresh TCP+TLS connection to the LLM host on every reply() /
reply_stream() -- the responder lives for a whole session (many turns), so a
per-call client pays repeated handshake latency that a persistent, keep-alive
client avoids."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.conversation.responder import OpenAICompatResponder


def _responder():
    return OpenAICompatResponder(
        base_url="http://llm", api_key="", model="test-model",
        system_prompt="You are a test assistant.", timeout=10.0,
    )


def _ok_client():
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"choices": [{"message": {"content": "ok"}}]})
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    return client


@pytest.mark.asyncio
async def test_reply_reuses_the_same_client_across_calls():
    with patch("httpx.AsyncClient", return_value=_ok_client()) as client_cls:
        responder = _responder()
        await responder.reply([{"role": "user", "content": "hi"}])
        await responder.reply([{"role": "user", "content": "again"}])
    assert client_cls.call_count == 1, "httpx.AsyncClient() must be constructed once, not per call"


@pytest.mark.asyncio
async def test_aclose_closes_the_underlying_client():
    mock_client = _ok_client()
    mock_client.aclose = AsyncMock()
    with patch("httpx.AsyncClient", return_value=mock_client):
        responder = _responder()
        await responder.reply([{"role": "user", "content": "hi"}])
        await responder.aclose()
    mock_client.aclose.assert_awaited_once()
