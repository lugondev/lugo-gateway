"""Tests for 2-phase tool calling in OpenAICompatResponder.reply_stream().

Phase 1 (detect): non-streaming POST with tools schema — checks for tool_calls.
Phase 2 (stream): once no tool_calls in response, stream final content normally.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.conversation.responder import OpenAICompatResponder
from app.services.conversation.tools.base import Tool, ToolContext, ToolRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _responder():
    return OpenAICompatResponder(
        base_url="http://llm",
        api_key="",
        model="test-model",
        system_prompt="You are a test assistant.",
        timeout=10.0,
    )


def _registry_with(*tools):
    reg = ToolRegistry()
    for t in tools:
        reg.add(t)
    return reg


def _tool(name: str, result: str = "ok"):
    async def run(args, ctx):
        return result

    return Tool(name=name, description="d", parameters={"type": "object"}, run=run)


def _make_aiter_lines(lines: list[str]):
    """Return a plain MagicMock that yields SSE lines as an async generator.

    httpx's aiter_lines() returns an async generator (not a coroutine), so the
    mock must use MagicMock (not AsyncMock) to avoid a 'coroutine is not async
    iterable' error when the code does ``async for line in resp.aiter_lines()``.
    """
    async def _gen():
        for line in lines:
            yield line

    return MagicMock(return_value=_gen())


def _non_stream_response(content: str | None = None, tool_calls: list | None = None):
    """Build a fake /chat/completions non-stream response dict."""
    message: dict = {"role": "assistant"}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    else:
        message["content"] = content or ""
    return {"choices": [{"message": message}]}


def _stream_lines(sentences: list[str]) -> list[str]:
    """Build SSE data lines for a list of complete sentences."""
    lines = []
    for s in sentences:
        data = {"choices": [{"delta": {"content": s}}]}
        lines.append(f"data: {json.dumps(data)}")
    lines.append("data: [DONE]")
    return lines


# ---------------------------------------------------------------------------
# No registry — existing streaming behaviour unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reply_stream_without_registry_streams_normally():
    """When no registry is passed, reply_stream uses the existing streaming path."""
    lines = _stream_lines(["Hello world."])
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.aiter_lines = _make_aiter_lines(lines)

    async def mock_stream(*args, **kwargs):
        return mock_resp

    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.stream = MagicMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        results = []
        async for chunk in _responder().reply_stream([{"role": "user", "content": "hi"}]):
            results.append(chunk)

    assert results  # got something back


# ---------------------------------------------------------------------------
# Registry present, no tool_calls — straight to streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reply_stream_with_registry_no_tool_calls_streams_directly():
    """detect phase: LLM returns plain content → skip tool loop, stream result."""
    reg = _registry_with(_tool("weather"))

    detect_resp = MagicMock()
    detect_resp.raise_for_status = MagicMock()
    detect_resp.json = MagicMock(return_value=_non_stream_response(content="It is sunny."))

    stream_lines = _stream_lines(["It is sunny."])
    stream_resp = MagicMock()
    stream_resp.raise_for_status = MagicMock()
    stream_resp.aiter_lines = _make_aiter_lines(stream_lines)
    stream_resp.__aenter__ = AsyncMock(return_value=stream_resp)
    stream_resp.__aexit__ = AsyncMock(return_value=False)

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=detect_resp)
    mock_client.stream = MagicMock(return_value=stream_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        results = []
        async for chunk in _responder().reply_stream(
            [{"role": "user", "content": "weather?"}],
            registry=reg,
            ctx=ToolContext(),
        ):
            results.append(chunk)

    # detect POST was called with tools in the payload
    call_kwargs = mock_client.post.call_args.kwargs
    assert "tools" in call_kwargs.get("json", {})
    assert results


# ---------------------------------------------------------------------------
# Registry present, tool_calls in detect response — tool loop runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reply_stream_runs_tool_and_streams_final():
    """detect → tool_call → registry.run → append tool result → stream final."""
    calls = []

    async def track_run(args, ctx):
        calls.append(args)
        return "Hanoi: 30°C"

    reg = _registry_with(
        Tool(name="weather", description="d", parameters={"type": "object"}, run=track_run)
    )

    tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "weather", "arguments": '{"city": "Hanoi"}'},
    }
    detect_resp = MagicMock()
    detect_resp.raise_for_status = MagicMock()
    # First detect: tool_calls; second detect (after tool result): plain content
    detect_resp.json = MagicMock(
        side_effect=[
            _non_stream_response(tool_calls=[tool_call]),
            _non_stream_response(content="The weather is 30°C."),
        ]
    )

    stream_lines = _stream_lines(["The weather is 30°C."])
    stream_resp = MagicMock()
    stream_resp.raise_for_status = MagicMock()
    stream_resp.aiter_lines = _make_aiter_lines(stream_lines)
    stream_resp.__aenter__ = AsyncMock(return_value=stream_resp)
    stream_resp.__aexit__ = AsyncMock(return_value=False)

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=detect_resp)
    mock_client.stream = MagicMock(return_value=stream_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        results = []
        async for chunk in _responder().reply_stream(
            [{"role": "user", "content": "weather Hanoi?"}],
            registry=reg,
            ctx=ToolContext(),
        ):
            results.append(chunk)

    # tool was called with the right args
    assert calls == [{"city": "Hanoi"}]
    # detect was called twice (once with tool_call, once clean)
    assert mock_client.post.call_count == 2
    # final stream produced content
    assert results


# ---------------------------------------------------------------------------
# max_iters safeguard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reply_stream_stops_tool_loop_at_max_iters():
    """If the LLM keeps requesting tools, stop after max_iters and stream anyway."""
    reg = _registry_with(_tool("loop_tool", result="result"))

    tool_call = {
        "id": "call_x",
        "type": "function",
        "function": {"name": "loop_tool", "arguments": "{}"},
    }
    # Always returns tool_calls — would loop forever without max_iters
    detect_resp = MagicMock()
    detect_resp.raise_for_status = MagicMock()
    detect_resp.json = MagicMock(return_value=_non_stream_response(tool_calls=[tool_call]))

    stream_lines = _stream_lines(["Fallback answer."])
    stream_resp = MagicMock()
    stream_resp.raise_for_status = MagicMock()
    stream_resp.aiter_lines = _make_aiter_lines(stream_lines)
    stream_resp.__aenter__ = AsyncMock(return_value=stream_resp)
    stream_resp.__aexit__ = AsyncMock(return_value=False)

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=detect_resp)
    mock_client.stream = MagicMock(return_value=stream_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        results = []
        async for chunk in _responder().reply_stream(
            [{"role": "user", "content": "loop"}],
            registry=reg,
            ctx=ToolContext(),
            max_iters=2,
        ):
            results.append(chunk)

    # detect called at most max_iters times (not infinite)
    assert mock_client.post.call_count <= 2
    # stream was still called (fell through to final answer)
    assert mock_client.stream.called

