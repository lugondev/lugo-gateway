from app.core.audio import wav_duration_seconds
from app.schemas.stt import STTResult
from app.services.stt.base import STTProvider


class _StubProvider(STTProvider):
    name = "stub"

    def __init__(self) -> None:
        self.received_wav: bytes | None = None

    async def transcribe_bytes(self, audio_bytes, language=None) -> STTResult:
        self.received_wav = audio_bytes
        return STTResult(engine=self.name, text="stub transcript", is_final=True)


async def test_buffering_stream_emits_no_partials_then_finalizes():
    provider = _StubProvider()
    stream = provider.open_stream(sample_rate=16000)

    # 0.5s of silence as PCM16 mono @16kHz.
    pcm = b"\x00\x00" * 8000
    assert await stream.accept(pcm[:4000]) == []
    assert await stream.accept(pcm[4000:]) == []

    result = await stream.finalize()
    assert result is not None
    assert result.text == "stub transcript"
    assert result.is_final is True

    # The provider received a valid WAV built from the buffered PCM.
    assert provider.received_wav[:4] == b"RIFF"
    assert abs(wav_duration_seconds(provider.received_wav) - 0.5) < 1e-3


async def test_buffering_stream_finalize_without_audio_returns_none():
    provider = _StubProvider()
    stream = provider.open_stream(sample_rate=16000)
    assert await stream.finalize() is None
