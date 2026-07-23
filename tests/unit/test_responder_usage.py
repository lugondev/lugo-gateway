import json
import pytest
from app.services.conversation.responder import OpenAICompatResponder


class _FakeStreamResp:
    def __init__(self, lines): self._lines = lines; self.status_code = 200
    def raise_for_status(self): pass
    async def aiter_lines(self):
        for ln in self._lines: yield ln
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


class _FakeClient:
    def __init__(self, lines): self._lines = lines
    def stream(self, *a, **k): return _FakeStreamResp(self._lines)
    async def aclose(self): pass


async def test_stream_captures_usage_chunk():
    lines = [
        'data: ' + json.dumps({"choices": [{"delta": {"content": "Hello."}}]}),
        'data: ' + json.dumps({"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 3}}),
        'data: [DONE]',
    ]
    r = OpenAICompatResponder(base_url="http://x/v1", api_key="", model="m", system_prompt="s", timeout=5.0)
    r._client = _FakeClient(lines)
    out = [chunk async for chunk in r._stream_history([{"role": "user", "content": "hi"}])]
    assert "".join(out).strip() == "Hello."
    assert r.last_usage == {"prompt_tokens": 12, "completion_tokens": 3}


async def test_stream_without_usage_leaves_none():
    lines = [
        'data: ' + json.dumps({"choices": [{"delta": {"content": "Hi."}}]}),
        'data: [DONE]',
    ]
    r = OpenAICompatResponder(base_url="http://x/v1", api_key="", model="m", system_prompt="s", timeout=5.0)
    r._client = _FakeClient(lines)
    _ = [c async for c in r._stream_history([{"role": "user", "content": "hi"}])]
    assert r.last_usage is None
