import base64
import io
import wave

import httpx
import pytest

from app.core.errors import ProviderError
from app.schemas.tts import TTSRequest
from app.services.tts.providers.http_tts_provider import HttpTtsProvider

_ENTRY = {
    "id": "t1", "kind": "tts", "engine": "http_tts", "model_id": "vieneu",
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
    provider = HttpTtsProvider(entry=_ENTRY)
    wav = await provider.render_wav(TTSRequest(text="xin chào", engine="http_tts", voice="v1"))

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
        "app.services.tts.providers.http_tts_provider.model_registry_store.find_enabled",
        fake_find_enabled,
    )
    wav = await HttpTtsProvider().render_wav(TTSRequest(text="hi", engine="http_tts"))
    assert wav == _WAV_BYTES
    assert captured["url"] == "http://tts-service:8100/v1/audio/speech"


@pytest.mark.asyncio
async def test_model_id_resolves_the_exact_registry_row(captured, monkeypatch):
    # Several rows can share engine "http_tts" pointing at different service
    # base URLs. A concrete model_id (the "engine|model_id" the picker sends)
    # must resolve that exact row via find(), not the non-deterministic
    # first-enabled fallback.
    seen = {}

    async def fake_find(kind, engine=None, model_id=None):
        seen["find"] = (kind, engine, model_id)
        return {**_ENTRY, "model_id": model_id, "base_url": "http://box-b:8100/v1"}

    async def fail_find_enabled(kind, engine=None):
        raise AssertionError("find_enabled must not be called when model_id is set")

    monkeypatch.setattr(
        "app.services.tts.providers.http_tts_provider.model_registry_store.find",
        fake_find,
    )
    monkeypatch.setattr(
        "app.services.tts.providers.http_tts_provider.model_registry_store.find_enabled",
        fail_find_enabled,
    )
    wav = await HttpTtsProvider().render_wav(
        TTSRequest(text="hi", engine="http_tts", model_id="vieneu-fly")
    )
    assert wav == _WAV_BYTES
    assert seen["find"] == ("tts", "http_tts", "vieneu-fly")
    assert captured["url"] == "http://box-b:8100/v1/audio/speech"
    assert '"model":"vieneu-fly"' in captured["json"]


@pytest.mark.asyncio
async def test_empty_model_id_falls_back_to_first_enabled(captured, monkeypatch):
    # Callers not yet migrated to row-based selection send an empty model_id;
    # the provider keeps the legacy first-enabled behaviour for them.
    async def fake_find_enabled(kind, engine=None):
        return _ENTRY

    async def fail_find(kind, engine=None, model_id=None):
        raise AssertionError("find must not be called when model_id is empty")

    monkeypatch.setattr(
        "app.services.tts.providers.http_tts_provider.model_registry_store.find_enabled",
        fake_find_enabled,
    )
    monkeypatch.setattr(
        "app.services.tts.providers.http_tts_provider.model_registry_store.find",
        fail_find,
    )
    wav = await HttpTtsProvider().render_wav(
        TTSRequest(text="hi", engine="http_tts")
    )
    assert wav == _WAV_BYTES
    assert captured["url"] == "http://tts-service:8100/v1/audio/speech"


@pytest.mark.asyncio
async def test_unconfigured_raises_provider_error(monkeypatch):
    async def fake_find_enabled(kind, engine=None):
        return None

    monkeypatch.setattr(
        "app.services.tts.providers.http_tts_provider.model_registry_store.find_enabled",
        fake_find_enabled,
    )
    # render_wav wraps everything as ProviderError (tts/base.py).
    with pytest.raises(ProviderError, match="not configured"):
        await HttpTtsProvider().render_wav(TTSRequest(text="hi", engine="http_tts"))


@pytest.mark.asyncio
async def test_http_error_becomes_provider_error(monkeypatch):
    transport = httpx.MockTransport(lambda r: httpx.Response(502, text="engine died"))
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **k: original(*a, **{**k, "transport": transport})
    )
    with pytest.raises(ProviderError, match="HTTP 502"):
        await HttpTtsProvider(entry=_ENTRY).render_wav(
            TTSRequest(text="hi", engine="http_tts")
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
        await HttpTtsProvider(entry=_ENTRY).render_wav(
            TTSRequest(text="hi", engine="http_tts")
        )


@pytest.mark.asyncio
async def test_timeout_comes_from_the_entry_config(captured):
    entry = {**_ENTRY, "config": {"timeout_seconds": 5.0}}
    provider = HttpTtsProvider(entry=entry)
    await provider.render_wav(TTSRequest(text="hi", engine="http_tts"))
    assert captured["timeout"] == 5.0


@pytest.mark.asyncio
async def test_timeout_falls_back_to_the_provider_default_when_entry_has_none(captured):
    # _ENTRY's config is {}, so no timeout_seconds override -- the provider's
    # own default (60.0, per _DEFAULT_TIMEOUT) must be what reaches httpx.
    provider = HttpTtsProvider(entry=_ENTRY)
    await provider.render_wav(TTSRequest(text="hi", engine="http_tts"))
    assert captured["timeout"] == 60.0


@pytest.mark.asyncio
async def test_a_configured_zero_timeout_is_not_discarded(captured):
    # `0 or self.timeout_seconds` would silently replace an explicit 0 with
    # the provider default -- a plain `is not None` check must not do that.
    entry = {**_ENTRY, "config": {"timeout_seconds": 0}}
    provider = HttpTtsProvider(entry=entry)
    await provider.render_wav(TTSRequest(text="hi", engine="http_tts"))
    assert captured["timeout"] == 0


def test_engine_is_registered():
    from app.services.tts.service import tts_service

    assert tts_service.get_provider("http_tts").name == "http_tts"


# ------------------------------------------------------------- voice capabilities


@pytest.fixture
def voices_handler(monkeypatch):
    """Mocks the remote model_service's GET {base_url}/voices -- the "schema"
    a deployed engine returns describing its presets and clone support."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.setdefault("calls", []).append(str(request.url))
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"label": "Host", "voice": "host"}],
                "supports_clone": True,
            },
        )

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **k: original(*a, **{**k, "transport": transport})
    )
    return seen


@pytest.mark.asyncio
async def test_list_voices_fetches_from_the_remote_voices_endpoint(voices_handler):
    provider = HttpTtsProvider(entry=_ENTRY)
    voices = await provider.list_voices()
    assert voices == [{"label": "Host", "voice": "host"}]
    assert voices_handler["calls"] == ["http://tts-service:8100/v1/voices"]


@pytest.mark.asyncio
async def test_supports_voice_clone_reads_the_remote_flag(voices_handler):
    provider = HttpTtsProvider(entry=_ENTRY)
    assert await provider.supports_voice_clone() is True


@pytest.mark.asyncio
async def test_list_voices_and_supports_voice_clone_share_one_remote_call(voices_handler):
    """A caller (the /v1/tts/voices route) asks both back-to-back -- that must
    not cost two round trips to the same base_url."""
    provider = HttpTtsProvider(entry=_ENTRY)
    await provider.list_voices()
    await provider.supports_voice_clone()
    assert len(voices_handler["calls"]) == 1


@pytest.mark.asyncio
async def test_list_voices_returns_empty_when_unconfigured(monkeypatch):
    async def fake_find_enabled(kind, engine=None):
        return None

    monkeypatch.setattr(
        "app.services.tts.providers.http_tts_provider.model_registry_store.find_enabled",
        fake_find_enabled,
    )
    provider = HttpTtsProvider()
    assert await provider.list_voices() == []
    assert await provider.supports_voice_clone() is False


@pytest.mark.asyncio
async def test_list_voices_returns_empty_when_remote_call_fails(monkeypatch):
    transport = httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **k: original(*a, **{**k, "transport": transport})
    )
    provider = HttpTtsProvider(entry=_ENTRY)
    # Voices are a UI nicety, not core synthesis -- a broken/unreachable
    # remote /voices endpoint must degrade to "no presets", not raise.
    assert await provider.list_voices() == []
    assert await provider.supports_voice_clone() is False


# --------------------------------------------------------- clone forwarding


@pytest.mark.asyncio
async def test_render_wav_forwards_ref_audio_as_base64(captured, tmp_path, monkeypatch):
    # TTSRequest.ref_audio_path is now validated against the artifacts dir
    # (task 5, 2026-07-28-critical-authz-fixes) -- point the schema's
    # artifact_store at tmp_path so this stays hermetic instead of writing
    # into the real artifacts/ dir.
    from app.services.artifacts import ArtifactStore

    fresh_store = ArtifactStore(str(tmp_path))
    monkeypatch.setattr("app.schemas.tts.artifact_store", fresh_store)

    ref_path = tmp_path / "ref.wav"
    ref_path.write_bytes(_WAV_BYTES)

    provider = HttpTtsProvider(entry=_ENTRY)
    await provider.render_wav(
        TTSRequest(
            text="xin chào", engine="http_tts",
            ref_audio_path=str(ref_path), ref_text="reference words",
        )
    )

    import json

    body = json.loads(captured["json"])
    assert base64.b64decode(body["ref_audio_base64"]) == _WAV_BYTES
    assert body["ref_text"] == "reference words"
    # The local path is meaningless on the remote host -- must not leak it.
    assert "ref_audio_path" not in body


@pytest.mark.asyncio
async def test_render_wav_omits_clone_fields_when_no_ref_audio(captured):
    provider = HttpTtsProvider(entry=_ENTRY)
    await provider.render_wav(TTSRequest(text="hi", engine="http_tts"))

    import json

    body = json.loads(captured["json"])
    assert "ref_audio_base64" not in body
    assert "ref_audio_path" not in body


def test_available_false_when_no_enabled_entry(monkeypatch):
    """Regression: this inherited TTSProvider.available()'s hardcoded True,
    so the admin dashboard reported http_tts usable with zero registry rows."""
    monkeypatch.setattr(
        "app.services.model_registry.store.model_registry_store.find_enabled_sync",
        lambda kind, engine=None: None,
    )
    assert HttpTtsProvider().available() is False


def test_available_true_when_enabled_entry_has_base_url(monkeypatch):
    monkeypatch.setattr(
        "app.services.model_registry.store.model_registry_store.find_enabled_sync",
        lambda kind, engine=None: dict(_ENTRY),
    )
    assert HttpTtsProvider().available() is True


def test_available_false_when_entry_has_blank_base_url(monkeypatch):
    monkeypatch.setattr(
        "app.services.model_registry.store.model_registry_store.find_enabled_sync",
        lambda kind, engine=None: {**_ENTRY, "base_url": "  "},
    )
    assert HttpTtsProvider().available() is False
