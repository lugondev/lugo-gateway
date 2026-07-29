import pytest

from app.core.errors import ProviderError
from app.schemas.tts import TTSRequest
from app.services.tts.base import RenderingTTSProvider


class _FakeProvider(RenderingTTSProvider):
    name = "fake"

    def __init__(self, wav: bytes = b"RIFF....WAVE", exc: Exception | None = None):
        self._wav = wav
        self._exc = exc

    async def _render_wav(self, payload: TTSRequest) -> bytes:
        if self._exc:
            raise self._exc
        return self._wav


@pytest.mark.asyncio
async def test_render_wav_returns_bytes_without_saving_an_artifact():
    provider = _FakeProvider(wav=b"RIFFbytes")
    assert await provider.render_wav(TTSRequest(text="xin chào", engine="fake")) == b"RIFFbytes"


@pytest.mark.asyncio
async def test_render_wav_wraps_failures_as_provider_error():
    provider = _FakeProvider(exc=RuntimeError("cuda oom"))
    with pytest.raises(ProviderError, match="fake synthesis failed: cuda oom"):
        await provider.render_wav(TTSRequest(text="xin chào", engine="fake"))


@pytest.mark.asyncio
async def test_render_audio_delegates_to_render_wav():
    """render_audio() must go through render_wav() (which wraps errors as
    ProviderError), not call _render_wav() directly."""
    provider = _FakeProvider(wav=b"RIFF_test_wav_data")
    audio_bytes, media_type = await provider.render_audio(TTSRequest(text="xin chào", engine="fake"))
    assert audio_bytes == b"RIFF_test_wav_data"
    assert media_type == "audio/wav"


@pytest.mark.asyncio
async def test_render_audio_still_wraps_errors_as_provider_error():
    provider = _FakeProvider(exc=RuntimeError("cuda oom"))
    with pytest.raises(ProviderError, match="fake synthesis failed: cuda oom"):
        await provider.render_audio(TTSRequest(text="xin chào", engine="fake"))


@pytest.mark.asyncio
async def test_list_voices_defaults_to_empty():
    """An engine that doesn't override list_voices() (no presets) must not
    crash callers that always await it -- the base class needs a concrete
    default, not an abstract/missing method."""
    provider = _FakeProvider()
    assert await provider.list_voices() == []


@pytest.mark.asyncio
async def test_supports_voice_clone_defaults_to_false():
    """An engine that doesn't override this must declare no clone support by
    default, so a client asking 'can I upload a reference clip here' gets a
    safe false rather than an AttributeError."""
    provider = _FakeProvider()
    assert await provider.supports_voice_clone() is False
