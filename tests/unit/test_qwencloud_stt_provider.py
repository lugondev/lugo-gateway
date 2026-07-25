import asyncio
import json

import httpx
import pytest

from app.schemas.stt import STTRequest
from app.services.stt.providers import qwencloud_provider as qc
from app.services.stt.providers.qwencloud_provider import (
    QwenCloudSttProvider,
    _family,
)
from app.services.stt.service import stt_service

_QWEN_ENTRY = {
    "id": "q1", "kind": "stt", "engine": "qwencloud", "model_id": "qwen3-asr-flash",
    "label": "QwenCloud", "enabled": True, "stage": "stable",
    "api_key": "sk-ws-test", "base_url": "https://dashscope-intl.aliyuncs.com",
    "config": {},
}

_MM_OK = {  # multimodal-generation success shape (verified live)
    "output": {"choices": [{"finish_reason": "stop", "message": {
        "annotations": [{"type": "audio_info", "emotion": "neutral", "language": "vi"}],
        "content": [{"text": "  xin chào  "}], "role": "assistant"}}]},
    "usage": {"total_tokens": 50},
}


@pytest.fixture
def captured(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["json"] = request.content
        return httpx.Response(200, json=_MM_OK)

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return seen


def test_family_detection():
    assert _family("qwen3-asr-flash") == "qwen3"
    assert _family("qwen3-asr-flash-realtime") == "qwen3"
    assert _family("fun-asr") == "funasr"
    assert _family("fun-asr-realtime") == "funasr"
    assert _family(None) == "qwen3"


@pytest.mark.asyncio
async def test_qwen3_batch_posts_multimodal_with_base64(captured, monkeypatch):
    async def fake_find(kind, engine, model_id):
        return _QWEN_ENTRY

    monkeypatch.setattr(
        "app.services.stt.providers.qwencloud_provider.model_registry_store.find", fake_find
    )
    result = await QwenCloudSttProvider().transcribe_bytes(b"RIFFDATA", "vi", "qwen3-asr-flash")

    assert captured["url"].endswith("/api/v1/services/aigc/multimodal-generation/generation")
    assert captured["auth"] == "Bearer sk-ws-test"
    body = captured["json"].decode()
    assert "data:audio/wav;base64," in body
    assert "UklGRkRBVEE" in body or "RIFFDATA" not in body  # base64 of b"RIFFDATA"
    assert '"language": "vi"' in body or '"language":"vi"' in body
    assert result.text == "xin chào"       # stripped
    assert result.engine == "qwencloud"
    assert result.is_final is True


@pytest.mark.asyncio
async def test_qwen3_batch_normalizes_compatible_mode_base_url(captured):
    entry = {**_QWEN_ENTRY, "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"}
    await QwenCloudSttProvider(entry=entry).transcribe_bytes(b"X")
    assert captured["url"] == (
        "https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
    )


@pytest.mark.asyncio
async def test_qwen3_batch_empty_output_yields_empty_text(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"output": {"choices": []}})
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **k: original(*a, **{**k, "transport": transport}))
    result = await QwenCloudSttProvider(entry=_QWEN_ENTRY).transcribe_bytes(b"X")
    assert result.text == ""


@pytest.mark.asyncio
async def test_unconfigured_entry_raises_clear_error(monkeypatch):
    async def fake_find_enabled(kind, engine=None):
        return None
    monkeypatch.setattr(
        "app.services.stt.providers.qwencloud_provider.model_registry_store.find_enabled",
        fake_find_enabled,
    )
    with pytest.raises(RuntimeError, match="not configured"):
        await QwenCloudSttProvider().transcribe_bytes(b"X")


@pytest.mark.asyncio
async def test_http_error_surfaces_status(monkeypatch):
    def handler(request):
        return httpx.Response(401, text="invalid api key")
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **k: original(*a, **{**k, "transport": transport}))
    with pytest.raises(RuntimeError, match="HTTP 401"):
        await QwenCloudSttProvider(entry=_QWEN_ENTRY).transcribe_bytes(b"X")


def test_engine_is_registered():
    assert stt_service.get_provider("qwencloud").name == "qwencloud"


def test_schema_accepts_the_engine():
    assert STTRequest(engine="qwencloud").engine == "qwencloud"


@pytest.mark.asyncio
async def test_list_engines_reports_qwencloud_remote():
    engines = await stt_service.list_engines()
    row = next(e for e in engines if e["engine"] == "qwencloud")
    assert row["mode"] == "remote"
    assert "configured" in row


class FakeWS:
    """Async-iterable fake websocket. Yields seeded server messages, records sends."""
    def __init__(self, incoming):
        self._incoming = list(incoming)   # list[str] server frames
        self.sent = []                    # list of frames the stream sent
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._incoming:
            raise StopAsyncIteration
        await asyncio.sleep(0)
        return self._incoming.pop(0)

    async def send(self, frame):
        self.sent.append(frame)

    async def close(self):
        self.closed = True


def _qwen_msgs():
    return [
        json.dumps({"type": "session.created"}),
        json.dumps({"type": "session.updated"}),
        json.dumps({"type": "conversation.item.input_audio_transcription.text",
                    "text": "", "stash": "xin"}),
        json.dumps({"type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "xin chào"}),
        json.dumps({"type": "session.finished"}),
    ]


@pytest.fixture
def fake_connect(monkeypatch):
    holder = {}

    async def _connect(url, headers):
        ws = FakeWS(holder["incoming"])
        holder["ws"] = ws
        holder["url"] = url
        holder["headers"] = headers
        return ws

    monkeypatch.setattr(qc, "_ws_connect", _connect)
    return holder


@pytest.mark.asyncio
async def test_qwen3_stream_maps_stash_and_transcript(fake_connect, monkeypatch):
    fake_connect["incoming"] = _qwen_msgs()
    monkeypatch.setattr(
        "app.services.stt.providers.qwencloud_provider.model_registry_store.find_enabled",
        lambda kind, engine=None: _async(_QWEN_ENTRY),
    )
    provider = QwenCloudSttProvider(entry=_QWEN_ENTRY)
    stream = provider.open_stream(sample_rate=16000, language="vi")

    results = await stream.accept(b"\x00\x00" * 160)
    # after connect+hello, drained partial(s)/final from the queue
    partials = [r for r in results if not r.is_final]
    finals = [r for r in results if r.is_final]
    assert any(r.text == "xin" for r in partials)
    assert any(r.text == "xin chào" for r in finals)

    # the hello (session.update) and a base64 append were sent
    assert any('"session.update"' in s for s in fake_connect["ws"].sent)
    assert any('"input_audio_buffer.append"' in s for s in fake_connect["ws"].sent)
    assert fake_connect["headers"]["Authorization"] == "Bearer sk-ws-test"
    assert "/api-ws/v1/realtime?model=qwen3-asr-flash-realtime" in fake_connect["url"]

    final = await stream.finalize()
    assert any('"session.finish"' in s for s in fake_connect["ws"].sent)
    assert fake_connect["ws"].closed is True


def _async(value):
    async def _c(*a, **k):
        return value
    return _c()
