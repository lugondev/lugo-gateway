import pytest
from unittest.mock import AsyncMock, patch

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


@pytest.mark.asyncio
async def test_synthesize_delegates_to_render_wav():
    """Verify that synthesize() goes through render_wav(), not _render_wav() directly.

    This pins the delegation that Task 2 establishes: render_wav() is the single
    real rendering entry point. synthesize() must call it (not _render_wav()).
    """
    provider = _FakeProvider(wav=b"RIFF_test_wav_data")

    # Spy on render_wav by patching it with an AsyncMock that calls the original
    original_render_wav = provider.render_wav
    provider.render_wav = AsyncMock(side_effect=original_render_wav)

    request = TTSRequest(text="xin chào", engine="fake")

    # Mock artifact_store.save_wav and wav_duration_seconds since we're only
    # testing delegation, not WAV processing
    with patch("app.services.tts.base.artifact_store.save_wav") as mock_save:
        with patch("app.services.tts.base.wav_duration_seconds") as mock_duration:
            mock_save.return_value = (None, "http://example.com/audio.wav")
            mock_duration.return_value = 1.5

            result = await provider.synthesize(request)

    # Assert render_wav was called exactly once with the request
    provider.render_wav.assert_called_once_with(request)

    # Assert the result carries expected fields
    assert result.engine == "fake"
    assert result.text == "xin chào"
    assert result.sample_rate == 24000
    assert result.audio_url == "http://example.com/audio.wav"
    assert result.duration_seconds == 1.5
