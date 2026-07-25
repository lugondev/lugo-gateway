import httpx
import pytest

from app.schemas.stt import STTRequest
from app.services.stt.providers.qwencloud_provider import (
    QwenCloudSttProvider,
    _family,
)

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
