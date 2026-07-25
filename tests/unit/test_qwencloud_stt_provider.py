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
    import base64 as _b64
    assert f"data:audio/wav;base64,{_b64.b64encode(b'RIFFDATA').decode()}" in body
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
    assert row["realtime"] is True


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

    # Production accept() yields once per call and returns whatever the reader
    # has already queued -- with the synchronous FakeWS double all frames are
    # buffered up front, so it's the TEST's job to keep calling accept() (as a
    # real caller feeding successive audio frames would) until the stream
    # ends, then finalize() to drain the tail.
    collected = []
    for _ in range(20):  # bounded; the fake ends well before this
        collected += await stream.accept(b"\x00\x00" * 160)
        if stream._done.is_set():
            break
    final = await stream.finalize()
    if final:
        collected.append(final)
    texts = [r.text for r in collected]
    assert "xin" in texts       # partial (from `stash`)
    assert "xin chào" in texts  # final (from `transcript`)

    # the hello (session.update) and a base64 append were sent
    assert any('"session.update"' in s for s in fake_connect["ws"].sent)
    assert any('"input_audio_buffer.append"' in s for s in fake_connect["ws"].sent)
    assert fake_connect["headers"]["Authorization"] == "Bearer sk-ws-test"
    assert "/api-ws/v1/realtime?model=qwen3-asr-flash-realtime" in fake_connect["url"]

    assert any('"session.finish"' in s for s in fake_connect["ws"].sent)
    assert fake_connect["ws"].closed is True


def _noop_msgs(n=1000):
    # A ready frame first (so the readiness gate in _ensure releases), then
    # frames that never trigger _is_done and never parse into a result --
    # keeps the reader "still running" (stream._done unset) across a couple
    # of accept() calls, without racing the reader's natural exhaustion.
    return [json.dumps({"type": "session.created"})] + [json.dumps({"type": "noop"})] * n


@pytest.mark.asyncio
async def test_qwen3_stream_accept_after_done_does_not_resend(fake_connect):
    fake_connect["incoming"] = []  # exhausts immediately -> reader sets _done fast
    provider = QwenCloudSttProvider(entry=_QWEN_ENTRY)
    stream = provider.open_stream(sample_rate=16000, language="vi")

    await stream.accept(b"\x00\x00" * 160)  # connects, sends hello + first append
    sent_before = len(fake_connect["ws"].sent)

    stream._done.set()  # simulate upstream having ended / socket dropped
    results = await stream.accept(b"\x00\x00" * 160)

    assert isinstance(results, list)
    # no new send was attempted against the dead socket
    assert len(fake_connect["ws"].sent) == sent_before


@pytest.mark.asyncio
async def test_qwen3_stream_accept_send_failure_raises_runtime_error(fake_connect):
    fake_connect["incoming"] = _noop_msgs()  # keeps the reader alive, _done unset
    provider = QwenCloudSttProvider(entry=_QWEN_ENTRY)
    stream = provider.open_stream(sample_rate=16000, language="vi")

    await stream.accept(b"\x00\x00" * 160)  # connects successfully
    assert not stream._done.is_set()

    async def _boom(frame):
        raise ConnectionError("socket closed")

    fake_connect["ws"].send = _boom

    with pytest.raises(RuntimeError, match="send failed"):
        await stream.accept(b"\x00\x00" * 160)


def _async(value):
    async def _c(*a, **k):
        return value
    return _c()


_FUNASR_ENTRY = {
    "id": "f1", "kind": "stt", "engine": "qwencloud", "model_id": "fun-asr-realtime",
    "label": "FunASR", "enabled": True, "stage": "stable",
    "api_key": "sk-ws-test", "base_url": "https://dashscope-intl.aliyuncs.com",
    "config": {"realtime_model": "fun-asr-realtime"},
}


def _funasr_msgs():
    return [
        json.dumps({"header": {"event": "task-started"}}),
        json.dumps({"header": {"event": "result-generated"},
                    "payload": {"output": {"sentence": {"text": "xin", "sentence_end": False}}}}),
        json.dumps({"header": {"event": "result-generated"},
                    "payload": {"output": {"sentence": {"text": "xin chào", "sentence_end": True}}}}),
        json.dumps({"header": {"event": "task-finished"}, "payload": {"output": {}}}),
    ]


@pytest.mark.asyncio
async def test_funasr_stream_maps_sentence_end(fake_connect):
    fake_connect["incoming"] = _funasr_msgs()
    stream = QwenCloudSttProvider(entry=_FUNASR_ENTRY).open_stream(sample_rate=16000, language="vi")

    # Mirrors test_qwen3_stream_maps_stash_and_transcript: accept() yields once
    # per call under the single-yield primitive, so aggregate across successive
    # calls until the reader signals done, then finalize() for the tail.
    collected = []
    for _ in range(20):  # bounded; the fake ends well before this
        collected += await stream.accept(b"\x00\x00" * 160)
        if stream._done.is_set():
            break
    texts = [r.text for r in collected]
    assert any(t == "xin" for t in texts) and any(
        r.text == "xin" and not r.is_final for r in collected)
    assert any(t == "xin chào" for t in texts) and any(
        r.text == "xin chào" and r.is_final for r in collected)

    # run-task text frame sent first, then a BINARY audio frame
    assert any(isinstance(s, str) and '"run-task"' in s for s in fake_connect["ws"].sent)
    assert any(isinstance(s, (bytes, bytearray)) for s in fake_connect["ws"].sent)
    assert "/api-ws/v1/inference" in fake_connect["url"]
    assert fake_connect["headers"]["Authorization"] == "bearer sk-ws-test"

    await stream.finalize()
    assert any(isinstance(s, str) and '"finish-task"' in s for s in fake_connect["ws"].sent)


@pytest.mark.asyncio
async def test_funasr_batch_one_shot_concatenates_finals(fake_connect, monkeypatch):
    fake_connect["incoming"] = _funasr_msgs()

    async def fake_find(kind, engine, model_id):
        return _FUNASR_ENTRY
    monkeypatch.setattr(
        "app.services.stt.providers.qwencloud_provider.model_registry_store.find", fake_find)

    # minimal valid WAV (44-byte header + a few PCM samples)
    import wave, io
    buf = io.BytesIO()
    w = wave.open(buf, "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes(b"\x00\x00" * 320); w.close()

    result = await QwenCloudSttProvider().transcribe_bytes(buf.getvalue(), "vi", "fun-asr-realtime")
    assert result.engine == "qwencloud"
    assert result.text == "xin chào"   # last/accumulated final
    assert result.is_final is True


@pytest.mark.asyncio
async def test_funasr_batch_captures_multiple_sentences(fake_connect, monkeypatch):
    # All audio is sent before the server emits results in a one-shot batch,
    # so both sentences arrive AFTER finish-task, during the drain -- not via
    # accept(). This proves drain_remaining_finals() doesn't drop the first one.
    fake_connect["incoming"] = [
        json.dumps({"header": {"event": "task-started"}}),
        json.dumps({"header": {"event": "result-generated"},
                    "payload": {"output": {"sentence": {"text": "câu một", "sentence_end": True}}}}),
        json.dumps({"header": {"event": "result-generated"},
                    "payload": {"output": {"sentence": {"text": "câu hai", "sentence_end": True}}}}),
        json.dumps({"header": {"event": "task-finished"}, "payload": {"output": {}}}),
    ]

    async def fake_find(kind, engine, model_id):
        return _FUNASR_ENTRY
    monkeypatch.setattr(
        "app.services.stt.providers.qwencloud_provider.model_registry_store.find", fake_find)

    import wave, io
    buf = io.BytesIO()
    w = wave.open(buf, "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes(b"\x00\x00" * 320); w.close()

    result = await QwenCloudSttProvider().transcribe_bytes(buf.getvalue(), "vi", "fun-asr-realtime")
    assert result.text == "câu một câu hai"
    assert result.is_final is True


@pytest.mark.asyncio
async def test_finalize_swallows_send_failure_on_dropped_socket(fake_connect):
    # A partial arrives, then the socket drops: finalize()'s finish-frame send
    # raises. finalize() must swallow it and return gracefully, never propagate.
    fake_connect["incoming"] = [
        json.dumps({"type": "conversation.item.input_audio_transcription.text", "stash": "xin ch"}),
    ]
    stream = QwenCloudSttProvider(entry=_QWEN_ENTRY).open_stream(16000, "vi")
    collected = []
    for _ in range(20):
        collected += await stream.accept(b"\x00\x00" * 160)
        if stream._done.is_set():
            break
    async def boom(_frame):
        raise ConnectionError("socket closed")
    stream._ws.send = boom            # the finish-frame send will now raise
    final = await stream.finalize()   # MUST NOT raise
    assert final is None or final.text == "xin ch"


@pytest.mark.asyncio
async def test_open_stream_connect_failure_raises_runtime_error(monkeypatch):
    # FIX 2: a websocket handshake/connect failure (not a RuntimeError) must be
    # translated to RuntimeError so the route's `except RuntimeError` catches it
    # and emits an error event instead of crashing the endpoint.
    async def _boom_connect(url, headers):
        raise ConnectionError("refused")

    monkeypatch.setattr(qc, "_ws_connect", _boom_connect)
    provider = QwenCloudSttProvider(entry=_QWEN_ENTRY)
    stream = provider.open_stream(sample_rate=16000, language="vi")

    with pytest.raises(RuntimeError, match="connect failed"):
        await stream.accept(b"\x00\x00" * 160)


@pytest.mark.asyncio
async def test_aclose_closes_socket_and_reader(fake_connect):
    # FIX 1: aclose() releases the upstream socket + reader task, and is
    # idempotent (safe to call again after the resources are already gone).
    fake_connect["incoming"] = _qwen_msgs()
    provider = QwenCloudSttProvider(entry=_QWEN_ENTRY)
    stream = provider.open_stream(sample_rate=16000, language="vi")

    await stream.accept(b"\x00\x00" * 160)  # connects the socket + reader
    await stream.aclose()
    assert fake_connect["ws"].closed is True

    await stream.aclose()  # idempotent -- must not raise
