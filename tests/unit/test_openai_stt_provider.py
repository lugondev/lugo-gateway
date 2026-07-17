import httpx
import pytest

from app.schemas.stt import STTRequest
from app.services.stt.providers.openai_stt_provider import OpenAICompatSttProvider
from app.services.stt.service import stt_service

_ENTRY = {
    "id": "e1", "kind": "stt", "engine": "openai_stt", "model_id": "phowhisper-medium",
    "label": "local box", "enabled": True, "stage": "stable",
    "api_key": "t0ken", "base_url": "http://stt-service:8100/v1", "config": {},
}


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
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return seen


@pytest.mark.asyncio
async def test_posts_to_the_entry_base_url_with_bearer(captured, monkeypatch):
    async def fake_find(kind, engine, model_id):
        return _ENTRY

    monkeypatch.setattr(
        "app.services.stt.providers.openai_stt_provider.model_registry_store.find", fake_find
    )
    provider = OpenAICompatSttProvider()
    result = await provider.transcribe_bytes(b"RIFFDATA", "vi", "phowhisper-medium")

    assert captured["url"] == "http://stt-service:8100/v1/audio/transcriptions"
    assert captured["auth"] == "Bearer t0ken"
    assert result.text == "xin chào"
    assert result.engine == "openai_stt"


@pytest.mark.asyncio
async def test_falls_back_to_the_enabled_entry_when_no_model_given(captured, monkeypatch):
    async def fake_find_enabled(kind, engine=None):
        return _ENTRY

    monkeypatch.setattr(
        "app.services.stt.providers.openai_stt_provider.model_registry_store.find_enabled",
        fake_find_enabled,
    )
    result = await OpenAICompatSttProvider().transcribe_bytes(b"RIFFDATA")
    assert result.text == "xin chào"


@pytest.mark.asyncio
async def test_explicit_entry_override_skips_the_registry(captured):
    # The registry's test-before-add call has no row to look up yet.
    provider = OpenAICompatSttProvider(entry=_ENTRY)
    await provider.transcribe_bytes(b"RIFFDATA")
    assert captured["auth"] == "Bearer t0ken"


@pytest.mark.asyncio
async def test_unconfigured_entry_raises_a_clear_error(monkeypatch):
    async def fake_find_enabled(kind, engine=None):
        return None

    monkeypatch.setattr(
        "app.services.stt.providers.openai_stt_provider.model_registry_store.find_enabled",
        fake_find_enabled,
    )
    with pytest.raises(RuntimeError, match="not configured"):
        await OpenAICompatSttProvider().transcribe_bytes(b"RIFFDATA")


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
        await OpenAICompatSttProvider(entry=_ENTRY).transcribe_bytes(b"RIFFDATA")


@pytest.mark.asyncio
async def test_timeout_comes_from_the_entry_config(captured, monkeypatch):
    entry = {**_ENTRY, "config": {"timeout_seconds": 5.0}}
    provider = OpenAICompatSttProvider(entry=entry)
    await provider.transcribe_bytes(b"RIFFDATA")  # must not raise


def test_engine_is_registered():
    assert stt_service.get_provider("openai_stt").name == "openai_stt"


def test_schema_accepts_the_engine():
    assert STTRequest(engine="openai_stt").engine == "openai_stt"


@pytest.mark.asyncio
async def test_list_engines_does_not_keyerror_on_the_new_engine(monkeypatch):
    # service.py's list_engines ends in `remote[engine]`, a dict keyed only by
    # whisper_service/eventlab -- a new engine must not fall into that branch.
    engines = await stt_service.list_engines()
    row = next(e for e in engines if e["engine"] == "openai_stt")
    assert row["mode"] == "remote"
