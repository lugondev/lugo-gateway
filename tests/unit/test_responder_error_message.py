"""LLMUnavailableError message must reflect the ACTUAL failure (base_url +
underlying reason), not stale, unconditional "start Ollama / set
CONVERSATION_LLM_BASE_URL" advice -- that env var is dead (Model Registry is
the only source of LLM config now) and the LLM may not even be Ollama."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core.errors import LLMUnavailableError
from app.services.conversation.responder import OpenAICompatResponder


def _responder(base_url="https://openrouter.ai/api/v1"):
    return OpenAICompatResponder(
        base_url=base_url, api_key="sk-test", model="deepseek/deepseek-v4-flash",
        system_prompt="You are a test assistant.", timeout=10.0,
    )


def _unauthorized_client():
    """An AsyncClient mock whose POST raises a 401 via raise_for_status."""
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx.Response(401, request=request, text="invalid api key")
    resp_mock = MagicMock()
    resp_mock.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("401 Unauthorized", request=request, response=response)
    )
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=resp_mock)
    return mock_client


@pytest.mark.asyncio
async def test_reply_error_names_base_url_and_reason_not_ollama():
    with patch("httpx.AsyncClient", return_value=_unauthorized_client()):
        with pytest.raises(LLMUnavailableError) as exc_info:
            await _responder().reply([{"role": "user", "content": "hi"}])
    message = str(exc_info.value)
    assert "Ollama" not in message
    assert "CONVERSATION_LLM_BASE_URL" not in message
    assert "https://openrouter.ai/api/v1" in message
    assert "401" in message
