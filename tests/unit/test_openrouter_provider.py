import base64

import pytest

from app.services.stt.providers.openrouter_provider import OpenRouterSttProvider
from app.services.system_config import SystemConfigStore


@pytest.fixture(autouse=True)
def _clean_store(tmp_path, monkeypatch):
    fresh = SystemConfigStore(str(tmp_path / "system_config.json"))
    monkeypatch.setattr("app.services.stt.providers.openrouter_provider.system_config_store", fresh)
    return fresh


@pytest.mark.asyncio
async def test_transcribe_raises_when_no_api_key_configured():
    provider = OpenRouterSttProvider(name="whisper_or", model="openai/whisper-large-v3-turbo")
    with pytest.raises(RuntimeError, match="not configured"):
        await provider.transcribe_bytes(b"fake wav bytes")


@pytest.mark.asyncio
async def test_transcribe_sends_correct_request_and_parses_response(monkeypatch, _clean_store):
    _clean_store.set_openrouter_api_key("sk-or-test")
    captured = {}

    async def fake_post(self, url, headers=None, json=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": "xin chào"}}]}

        return R()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    provider = OpenRouterSttProvider(name="qwen3_asr_or", model="qwen/qwen3-asr-flash-2026-02-10")
    result = await provider.transcribe_bytes(b"fake wav bytes")

    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-or-test"
    assert captured["json"]["model"] == "qwen/qwen3-asr-flash-2026-02-10"

    content = captured["json"]["messages"][0]["content"]
    audio_part = next(p for p in content if p["type"] == "input_audio")
    assert audio_part["input_audio"]["format"] == "wav"
    assert base64.b64decode(audio_part["input_audio"]["data"]) == b"fake wav bytes"

    assert result.engine == "qwen3_asr_or"
    assert result.text == "xin chào"
    assert result.is_final is True


@pytest.mark.asyncio
async def test_transcribe_strips_whitespace_from_response(monkeypatch, _clean_store):
    _clean_store.set_openrouter_api_key("sk-or-test")

    async def fake_post(self, url, headers=None, json=None):
        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": "  hello world  \n"}}]}

        return R()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    provider = OpenRouterSttProvider(name="whisper_or", model="openai/whisper-large-v3-turbo")
    result = await provider.transcribe_bytes(b"fake wav bytes")
    assert result.text == "hello world"


@pytest.mark.asyncio
async def test_transcribe_wraps_http_status_error(monkeypatch, _clean_store):
    _clean_store.set_openrouter_api_key("sk-or-test")

    async def fake_post(self, url, headers=None, json=None):
        import httpx

        request = httpx.Request("POST", url)
        response = httpx.Response(401, text="invalid api key", request=request)

        class R:
            def raise_for_status(self):
                raise httpx.HTTPStatusError("401", request=request, response=response)

        return R()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    provider = OpenRouterSttProvider(name="whisper_or", model="openai/whisper-large-v3-turbo")
    with pytest.raises(RuntimeError, match="whisper_or returned HTTP 401"):
        await provider.transcribe_bytes(b"fake wav bytes")
