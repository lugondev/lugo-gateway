import re

import httpx
import pytest

from app.schemas.stt import STTRequest
from app.services.stt.providers.http_stt_provider import HttpSttProvider
from app.services.stt.service import stt_service

_ENTRY = {
    "id": "e1", "kind": "stt", "engine": "http_stt", "model_id": "large-v3-turbo",
    "label": "local box", "enabled": True, "stage": "stable",
    "api_key": "t0ken", "base_url": "http://stt-service:8100/v1", "config": {},
}


def _multipart_field(body: bytes, name: str) -> str | None:
    """Pull a form field's value out of a raw multipart/form-data body."""
    match = re.search(rf'name="{name}"\r\n\r\n(.*?)\r\n--'.encode(), body, re.DOTALL)
    return match.group(1).decode() if match else None


@pytest.fixture
def captured(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = request.content
        return httpx.Response(200, json={"text": " xin chào  "})

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return seen


@pytest.mark.asyncio
async def test_posts_to_the_entry_base_url_with_bearer(captured, monkeypatch):
    async def fake_find(kind, engine, model_id):
        return _ENTRY

    monkeypatch.setattr(
        "app.services.stt.providers.http_stt_provider.model_registry_store.find", fake_find
    )
    provider = HttpSttProvider()
    result = await provider.transcribe_bytes(b"RIFFDATA", "vi", "large-v3-turbo")

    assert captured["url"] == "http://stt-service:8100/v1/audio/transcriptions"
    assert captured["auth"] == "Bearer t0ken"
    assert _multipart_field(captured["body"], "model") == "large-v3-turbo"
    assert _multipart_field(captured["body"], "language") == "vi"
    assert result.text == "xin chào"
    assert result.engine == "http_stt"


@pytest.mark.asyncio
async def test_falls_back_to_the_enabled_entry_when_no_model_given(captured, monkeypatch):
    async def fake_find_enabled(kind, engine=None):
        return _ENTRY

    monkeypatch.setattr(
        "app.services.stt.providers.http_stt_provider.model_registry_store.find_enabled",
        fake_find_enabled,
    )
    result = await HttpSttProvider().transcribe_bytes(b"RIFFDATA")
    assert result.text == "xin chào"


@pytest.mark.asyncio
async def test_explicit_entry_override_skips_the_registry(captured):
    # The registry's test-before-add call has no row to look up yet.
    provider = HttpSttProvider(entry=_ENTRY)
    await provider.transcribe_bytes(b"RIFFDATA")
    assert captured["auth"] == "Bearer t0ken"


@pytest.mark.asyncio
async def test_unconfigured_entry_raises_a_clear_error(monkeypatch):
    async def fake_find_enabled(kind, engine=None):
        return None

    monkeypatch.setattr(
        "app.services.stt.providers.http_stt_provider.model_registry_store.find_enabled",
        fake_find_enabled,
    )
    with pytest.raises(RuntimeError, match="not configured"):
        await HttpSttProvider().transcribe_bytes(b"RIFFDATA")


@pytest.mark.asyncio
async def test_http_error_surfaces_the_status_and_body(monkeypatch):
    def handler(request):
        return httpx.Response(401, text="invalid bearer token")

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **k: original(*a, **{**k, "transport": transport})
    )
    with pytest.raises(RuntimeError, match="HTTP 401"):
        await HttpSttProvider(entry=_ENTRY).transcribe_bytes(b"RIFFDATA")


@pytest.mark.asyncio
async def test_timeout_comes_from_the_entry_config(captured, monkeypatch):
    entry = {**_ENTRY, "config": {"timeout_seconds": 5.0}}
    provider = HttpSttProvider(entry=entry)
    await provider.transcribe_bytes(b"RIFFDATA")
    assert captured["timeout"] == 5.0


@pytest.mark.asyncio
async def test_timeout_falls_back_to_the_provider_default_when_entry_has_none(captured):
    # _ENTRY's config is {}, so no timeout_seconds override -- the provider's
    # own default (60.0, per _DEFAULT_TIMEOUT) must be what reaches httpx.
    provider = HttpSttProvider(entry=_ENTRY)
    await provider.transcribe_bytes(b"RIFFDATA")
    assert captured["timeout"] == 60.0


@pytest.mark.asyncio
async def test_a_configured_zero_timeout_is_not_discarded(captured):
    # `0 or self.timeout_seconds` would silently replace an explicit 0 with
    # the provider default -- a plain `is not None` check must not do that.
    entry = {**_ENTRY, "config": {"timeout_seconds": 0}}
    provider = HttpSttProvider(entry=entry)
    await provider.transcribe_bytes(b"RIFFDATA")
    assert captured["timeout"] == 0


def test_engine_is_registered():
    assert stt_service.get_provider("http_stt").name == "http_stt"


def test_schema_accepts_the_engine():
    assert STTRequest(engine="http_stt").engine == "http_stt"


@pytest.mark.asyncio
async def test_list_engines_does_not_keyerror_on_the_new_engine(monkeypatch):
    # service.py's list_engines ends in `remote[engine]`, a dict keyed only by
    # whisper_service/eventlab -- a new engine must not fall into that branch.
    engines = await stt_service.list_engines()
    row = next(e for e in engines if e["engine"] == "http_stt")
    assert row["mode"] == "remote"
