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
async def test_synthesize_still_wraps_errors_as_provider_error():
    provider = _FakeProvider(exc=RuntimeError("cuda oom"))
    with pytest.raises(ProviderError, match="fake synthesis failed: cuda oom"):
        await provider.synthesize(TTSRequest(text="xin chào", engine="fake"))
