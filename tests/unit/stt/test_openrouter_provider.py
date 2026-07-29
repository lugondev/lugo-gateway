import base64

import pytest

from app.services.model_registry.store import model_registry_store
from app.services.stt.providers.openrouter_provider import OpenRouterSttProvider


@pytest.mark.asyncio
async def test_transcribe_raises_when_no_registry_entry_or_key():
    provider = OpenRouterSttProvider(name="whisper_or", model="openai/whisper-large-v3-turbo")
    with pytest.raises(RuntimeError, match="not configured"):
        await provider.transcribe_bytes(b"fake wav bytes")


@pytest.mark.asyncio
async def test_transcribe_uses_explicit_api_key_override_without_registry_lookup(monkeypatch):
    """The Model Registry's test-before-add flow constructs the provider with
    an explicit api_key (the entry doesn't exist yet, so there's nothing to
    look up) -- this must be used as-is, with no registry query at all."""
    captured = {}

    async def fake_post(self, url, headers=None, json=None):
        captured["headers"] = headers

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"text": "ok"}

        return R()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    provider = OpenRouterSttProvider(
        name="whisper_or", model="openai/whisper-large-v3-turbo", api_key="sk-or-override"
    )
    await provider.transcribe_bytes(b"fake wav bytes")
    assert captured["headers"]["Authorization"] == "Bearer sk-or-override"


@pytest.mark.asyncio
async def test_transcribe_looks_up_api_key_from_matching_registry_entry(monkeypatch):
    await model_registry_store.create(
        "stt", "qwen3_asr_or", "qwen/qwen3-asr-flash-2026-02-10", "Qwen3 ASR Flash", api_key="sk-or-test"
    )
    captured = {}

    async def fake_post(self, url, headers=None, json=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"text": "xin chào"}

        return R()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    provider = OpenRouterSttProvider(name="qwen3_asr_or", model="qwen/qwen3-asr-flash-2026-02-10")
    result = await provider.transcribe_bytes(b"fake wav bytes")

    assert captured["url"] == "https://openrouter.ai/api/v1/audio/transcriptions"
    assert captured["headers"]["Authorization"] == "Bearer sk-or-test"
    assert captured["json"]["model"] == "qwen/qwen3-asr-flash-2026-02-10"

    audio_part = captured["json"]["input_audio"]
    assert audio_part["format"] == "wav"
    assert base64.b64decode(audio_part["data"]) == b"fake wav bytes"

    assert result.engine == "qwen3_asr_or"
    assert result.text == "xin chào"
    assert result.is_final is True


@pytest.mark.asyncio
async def test_transcribe_does_not_use_a_different_engines_key():
    """Per-model keys: configuring whisper_or's key must not make
    qwen3_asr_or usable -- each engine/model is keyed independently."""
    await model_registry_store.create(
        "stt", "whisper_or", "openai/whisper-large-v3-turbo", "Whisper v3 Turbo", api_key="sk-or-whisper-key"
    )
    provider = OpenRouterSttProvider(name="qwen3_asr_or", model="qwen/qwen3-asr-flash-2026-02-10")
    with pytest.raises(RuntimeError, match="not configured"):
        await provider.transcribe_bytes(b"fake wav bytes")


@pytest.mark.asyncio
async def test_transcribe_strips_whitespace_from_response(monkeypatch):
    await model_registry_store.create(
        "stt", "whisper_or", "openai/whisper-large-v3-turbo", "Whisper v3 Turbo", api_key="sk-or-test"
    )

    async def fake_post(self, url, headers=None, json=None):
        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"text": "  hello world  \n"}

        return R()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    provider = OpenRouterSttProvider(name="whisper_or", model="openai/whisper-large-v3-turbo")
    result = await provider.transcribe_bytes(b"fake wav bytes")
    assert result.text == "hello world"


@pytest.mark.asyncio
async def test_transcribe_passes_language_hint_when_given(monkeypatch):
    await model_registry_store.create(
        "stt", "whisper_or", "openai/whisper-large-v3-turbo", "Whisper v3 Turbo", api_key="sk-or-test"
    )
    captured = {}

    async def fake_post(self, url, headers=None, json=None):
        captured["json"] = json

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"text": "hola"}

        return R()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    provider = OpenRouterSttProvider(name="whisper_or", model="openai/whisper-large-v3-turbo")
    await provider.transcribe_bytes(b"fake wav bytes", language="es")

    assert captured["json"]["language"] == "es"


@pytest.mark.asyncio
async def test_transcribe_omits_language_field_when_not_given(monkeypatch):
    await model_registry_store.create(
        "stt", "whisper_or", "openai/whisper-large-v3-turbo", "Whisper v3 Turbo", api_key="sk-or-test"
    )
    captured = {}

    async def fake_post(self, url, headers=None, json=None):
        captured["json"] = json

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"text": "hi"}

        return R()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    provider = OpenRouterSttProvider(name="whisper_or", model="openai/whisper-large-v3-turbo")
    await provider.transcribe_bytes(b"fake wav bytes")

    assert "language" not in captured["json"]


@pytest.mark.asyncio
async def test_transcribe_wraps_http_status_error(monkeypatch):
    await model_registry_store.create(
        "stt", "whisper_or", "openai/whisper-large-v3-turbo", "Whisper v3 Turbo", api_key="sk-or-test"
    )

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
