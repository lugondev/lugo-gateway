from app.core.audio import wav_duration_seconds
from app.schemas.stt import STTResult
from app.services.stt.base import STTProvider


class _StubProvider(STTProvider):
    name = "stub"

    def __init__(self) -> None:
        self.received_wav: bytes | None = None
        # One entry per provider call, holding the byte length it was handed: a
        # paid engine bills per call, so the count and the sizes are the spend.
        self.calls: list[int] = []

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        self.received_wav = audio_bytes
        self.calls.append(len(audio_bytes))
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


async def test_a_second_finalize_does_not_re_send_the_already_transcribed_audio():
    """A `flush` is the end of an utterance, so the audio it transcribed is
    spent. Repeating `flush` must not hand the same bytes to the provider
    again: on any non-native-streaming engine (http_stt, the OpenRouter
    engines, qwencloud batch) each call is a real paid request, while the
    socket meters only the audio that arrived since the last flush -- so the
    second call would be spend the metering cannot see. It also used to return
    the first utterance's transcript a second time.
    """
    provider = _StubProvider()
    stream = provider.open_stream(sample_rate=16000)
    await stream.accept(b"\x00\x00" * 16000)  # 1 s @ 16 kHz

    first = await stream.finalize()
    assert first is not None
    assert provider.calls == [len(provider.received_wav)]
    spent = list(provider.calls)

    second = await stream.finalize()
    assert provider.calls == spent, (
        "a second finalize re-sent audio the provider was already paid for: "
        f"calls {provider.calls} (bytes per call)"
    )
    assert second is None, "no new audio arrived, so there is no new transcript"


async def test_a_flush_after_more_audio_transcribes_only_the_new_audio():
    """Clearing on finalize must not break the real streaming contract: audio
    that arrives after a flush is a new utterance and still gets transcribed --
    but only that audio, not the earlier utterance again."""
    provider = _StubProvider()
    stream = provider.open_stream(sample_rate=16000)

    await stream.accept(b"\x00\x00" * 16000)  # 1 s
    assert await stream.finalize() is not None
    first_bytes = provider.calls[0]

    await stream.accept(b"\x00\x00" * 8000)  # 0.5 s
    assert await stream.finalize() is not None
    assert len(provider.calls) == 2
    assert abs(wav_duration_seconds(provider.received_wav) - 0.5) < 1e-3
    assert provider.calls[1] < first_bytes, (
        "the second flush must carry only the new audio"
    )
