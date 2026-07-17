import io
import wave

import httpx
import pytest

from app.core.errors import ProviderError
from app.schemas.tts import TTSRequest
from app.services.tts.providers.openai_tts_provider import OpenAICompatTTSProvider

_ENTRY = {
    "id": "t1", "kind": "tts", "engine": "openai_tts", "model_id": "vieneu",
    "label": "local box", "enabled": True, "stage": "stable",
    "api_key": "t0ken", "base_url": "http://tts-service:8100/v1", "config": {},
}


def _tiny_wav() -> bytes:
    """A minimal, real WAV payload -- valid enough to pass the provider's
    RIFF/WAVE sniff, unlike the placeholder b"RIFFWAVEDATA" this fixture used
    to return (which starts with RIFF but has "DATA", not "WAVE", at offset 8
    and would now be rejected)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(8000)
        f.writeframes(b"\x00\x00")
    return buf.getvalue()


_WAV_BYTES = _tiny_wav()


@pytest.fixture
def captured(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["json"] = request.read().decode()
        return httpx.Response(200, content=_WAV_BYTES)

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return seen


@pytest.mark.asyncio
async def test_posts_to_audio_speech_and_returns_wav_bytes(captured):
    provider = OpenAICompatTTSProvider(entry=_ENTRY)
    wav = await provider.render_wav(TTSRequest(text="xin chào", engine="openai_tts", voice="v1"))

    assert wav == _WAV_BYTES
    assert captured["url"] == "http://tts-service:8100/v1/audio/speech"
    assert captured["auth"] == "Bearer t0ken"
    # Pin what actually goes on the wire: model, input text and voice.
    assert '"model":"vieneu"' in captured["json"]
    assert '"input":"xin chào"' in captured["json"]
    assert '"voice":"v1"' in captured["json"]


@pytest.mark.asyncio
async def test_resolves_the_enabled_entry_from_the_registry(captured, monkeypatch):
    async def fake_find_enabled(kind, engine=None):
        return _ENTRY

    monkeypatch.setattr(
        "app.services.tts.providers.openai_tts_provider.model_registry_store.find_enabled",
        fake_find_enabled,
    )
    wav = await OpenAICompatTTSProvider().render_wav(TTSRequest(text="hi", engine="openai_tts"))
    assert wav == _WAV_BYTES
    assert captured["url"] == "http://tts-service:8100/v1/audio/speech"


@pytest.mark.asyncio
async def test_unconfigured_raises_provider_error(monkeypatch):
    async def fake_find_enabled(kind, engine=None):
        return None

    monkeypatch.setattr(
        "app.services.tts.providers.openai_tts_provider.model_registry_store.find_enabled",
        fake_find_enabled,
    )
    # render_wav wraps everything as ProviderError (tts/base.py).
    with pytest.raises(ProviderError, match="not configured"):
        await OpenAICompatTTSProvider().render_wav(TTSRequest(text="hi", engine="openai_tts"))


@pytest.mark.asyncio
async def test_http_error_becomes_provider_error(monkeypatch):
    transport = httpx.MockTransport(lambda r: httpx.Response(502, text="engine died"))
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **k: original(*a, **{**k, "transport": transport})
    )
    with pytest.raises(ProviderError, match="HTTP 502"):
        await OpenAICompatTTSProvider(entry=_ENTRY).render_wav(
            TTSRequest(text="hi", engine="openai_tts")
        )


@pytest.mark.asyncio
async def test_non_wav_200_response_becomes_a_clean_provider_error(monkeypatch):
    # A 200 that isn't actually a WAV (a JSON error envelope, an MP3, a
    # truncated body) must not sail past raise_for_status() and reach the
    # Opus hot path's wave.open() as a bare wave.Error.
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"error": "engine returned nonsense"})
    )
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **k: original(*a, **{**k, "transport": transport})
    )
    with pytest.raises(ProviderError, match="not a WAV file"):
        await OpenAICompatTTSProvider(entry=_ENTRY).render_wav(
            TTSRequest(text="hi", engine="openai_tts")
        )


@pytest.mark.asyncio
async def test_timeout_comes_from_the_entry_config(captured):
    entry = {**_ENTRY, "config": {"timeout_seconds": 5.0}}
    provider = OpenAICompatTTSProvider(entry=entry)
    await provider.render_wav(TTSRequest(text="hi", engine="openai_tts"))
    assert captured["timeout"] == 5.0


@pytest.mark.asyncio
async def test_timeout_falls_back_to_the_provider_default_when_entry_has_none(captured):
    # _ENTRY's config is {}, so no timeout_seconds override -- the provider's
    # own default (60.0, per _DEFAULT_TIMEOUT) must be what reaches httpx.
    provider = OpenAICompatTTSProvider(entry=_ENTRY)
    await provider.render_wav(TTSRequest(text="hi", engine="openai_tts"))
    assert captured["timeout"] == 60.0


@pytest.mark.asyncio
async def test_a_configured_zero_timeout_is_not_discarded(captured):
    # `0 or self.timeout_seconds` would silently replace an explicit 0 with
    # the provider default -- a plain `is not None` check must not do that.
    entry = {**_ENTRY, "config": {"timeout_seconds": 0}}
    provider = OpenAICompatTTSProvider(entry=entry)
    await provider.render_wav(TTSRequest(text="hi", engine="openai_tts"))
    assert captured["timeout"] == 0


def test_engine_is_registered():
    from app.services.tts.service import tts_service

    assert tts_service.get_provider("openai_tts").name == "openai_tts"
