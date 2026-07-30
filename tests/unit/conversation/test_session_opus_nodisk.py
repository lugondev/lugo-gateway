"""Pins the no-disk seam for TTS in ConversationSession, in BOTH downlink modes.

_synth()/speak() used to call synthesize() (which writes a WAV to the artifact
store) and then, for Opus mode, immediately read the file back to decode it for
Opus encoding -- the artifact URL was never used once packets existed. A
separate "url" transport mode existed purely to hand that same artifact URL to
the browser to fetch.

Both are gone. `TTSProvider.render_audio(payload) -> (audio_bytes, media_type)`
is now the ONLY seam the session core calls: RenderingTTSProvider implements it
via render_wav() (real synthesis, WAV bytes, no artifact side effect); a plain
TTSProvider (e.g. edge_tts) implements it directly. Opus mode decodes those
bytes to PCM16 and encodes Opus packets; wav mode (the new default) pushes them
straight over the wire as one binary frame per sentence. Neither path ever
writes to or reads from the artifact store.

Follows the harness pattern established by test_conversation_session_core.py
(ConversationSession driven directly with stub providers, no WS transport).
"""

import pytest

from app.core.audio import pcm16_to_wav_bytes
from audio_helpers import _tone_mp3
from app.core.opus import opus_available
from app.schemas.stt import STTResult
from app.schemas.tts import TTSRequest
from app.services.artifacts import artifact_store
from app.services.conversation import session as session_module
from app.services.conversation.session import ConversationSession, SessionRuntimeConfig
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import RenderingTTSProvider, TTSProvider
from app.services.tts.service import tts_service

SR = 16000
OUT_SR = 24000

pytestmark = pytest.mark.skipif(
    not opus_available(), reason="opuslib/libopus not loadable on this host"
)


def _silence_wav(ms: int = 100, sr: int = OUT_SR) -> bytes:
    n = int(sr * ms / 1000)
    return pcm16_to_wav_bytes(b"\x00\x00" * n, sample_rate=sr)


class _StubSTT(STTProvider):
    name = "stub-opus-nodisk-stt"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chao", is_final=True)


class _FakeRenderingTTS(RenderingTTSProvider):
    """A real RenderingTTSProvider: exercises the actual render_wav()/render_audio()
    delegation from app.services.tts.base, not a hand-rolled double."""

    name = "stub-opus-nodisk-render-tts"
    sample_rate = OUT_SR

    def __init__(self, wav: bytes):
        self._wav = wav
        self.render_wav_calls = 0

    async def _render_wav(self, payload: TTSRequest) -> bytes:
        self.render_wav_calls += 1
        return self._wav


class _NonRenderingTTS(TTSProvider):
    """Stands in for edge_tts: a plain TTSProvider (not a RenderingTTSProvider),
    so it has no render_wav() at all -- only render_audio(), the only seam
    every engine implements now."""

    name = "stub-opus-nodisk-nonrender-tts"

    def __init__(self, wav: bytes):
        self._wav = wav

    async def render_audio(self, payload: TTSRequest) -> tuple[bytes, str]:
        return self._wav, "audio/mpeg"


@pytest.fixture(autouse=True)
def _stubs():
    stt_service.providers["stub-opus-nodisk-stt"] = _StubSTT()
    yield
    stt_service.providers.pop("stub-opus-nodisk-stt", None)
    tts_service.providers.pop("stub-opus-nodisk-render-tts", None)
    tts_service.providers.pop("stub-opus-nodisk-nonrender-tts", None)


def _cfg(**over):
    base = dict(
        session_id="s1", profile_name=None, stt_engine="stub-opus-nodisk-stt", language="vi",
        tts_engine="stub-opus-nodisk-render-tts", voice=None, ref_audio_path=None, ref_text=None,
        tts_instruct=None, tts_speed=None, tts_language=None, sample_rate=SR,
        output_sample_rate=OUT_SR, audio_codec="pcm16", want_audio=True, want_text=False,
        audio_out="opus", denoise=False, resume_sid=None,
    )
    base.update(over)
    return SessionRuntimeConfig(**base)


async def _drive_text_turn(cfg) -> tuple[ConversationSession, list, list]:
    events: list = []
    audio_pkts: list = []

    async def emit(name, **p):
        events.append((name, p))

    async def emit_audio(pkt):
        audio_pkts.append(pkt)

    sess = ConversationSession(cfg, emit, emit_audio)
    await sess.start()
    await sess.feed_text("hi")
    await sess.wait_current_turn()
    await sess.close()
    return sess, events, audio_pkts


@pytest.mark.asyncio
async def test_opus_mode_calls_render_wav_and_writes_no_artifact():
    fake_wav = _silence_wav()
    provider = _FakeRenderingTTS(fake_wav)
    tts_service.providers["stub-opus-nodisk-render-tts"] = provider

    before = set(artifact_store.base_dir.iterdir())
    sess, events, audio_pkts = await _drive_text_turn(_cfg())

    assert sess.opus_encoder is not None  # sanity: opus mode actually engaged
    assert provider.render_wav_calls >= 1
    # the load-bearing property: no artifact written -- render_audio() is the
    # only seam, and it never touches the artifact store.
    assert set(artifact_store.base_dir.iterdir()) == before
    assert audio_pkts  # opus packets did get emitted
    names = [n for n, _ in events]
    assert "audio_start" in names
    assert "audio_end" in names
    assert "audio_chunk" not in names


@pytest.mark.asyncio
async def test_opus_encoder_receives_exactly_the_bytes_render_wav_returned(monkeypatch):
    fake_wav = _silence_wav()
    provider = _FakeRenderingTTS(fake_wav)
    tts_service.providers["stub-opus-nodisk-render-tts"] = provider

    decode_calls: list = []
    original_wav_bytes_to_pcm16 = session_module.wav_bytes_to_pcm16

    def spy_wav_bytes_to_pcm16(wav_bytes, target_sr):
        decode_calls.append(wav_bytes)
        return original_wav_bytes_to_pcm16(wav_bytes, target_sr)

    monkeypatch.setattr(session_module, "wav_bytes_to_pcm16", spy_wav_bytes_to_pcm16)

    _, _, audio_pkts = await _drive_text_turn(_cfg())

    assert decode_calls  # the decode step actually ran
    assert decode_calls[0] == fake_wav  # the exact bytes render_wav() returned
    assert audio_pkts


@pytest.mark.asyncio
async def test_wav_mode_pushes_one_binary_frame_per_sentence():
    fake_wav = _silence_wav()
    provider = _FakeRenderingTTS(fake_wav)
    tts_service.providers["stub-opus-nodisk-render-tts"] = provider

    _session, events, audio_frames = await _drive_text_turn(
        _cfg(audio_out="wav", tts_engine="stub-opus-nodisk-render-tts")
    )

    assert not [p for n, p in events if n == "audio_chunk"]  # event is gone
    starts = [p for n, p in events if n == "audio_start"]
    ends = [p for n, p in events if n == "audio_end"]
    # EchoResponder (the default no-LLM responder driving _drive_text_turn)
    # always replies with exactly 3 sentences (see its fixed reply string in
    # responder.py) -- pinned literally, not just relationally, so a
    # regression that only emits audio for e.g. 1 of the 3 sentences still
    # fails here. `want_text=False` in `_cfg` means there's no `response_text`
    # to anchor the sentence count against instead.
    assert len(starts) == 3
    assert len(ends) == 3
    assert len(audio_frames) == 3  # one binary WAV frame per sentence
    assert all(s["codec"] == "wav" for s in starts)
    assert all(frame[:4] == b"RIFF" for frame in audio_frames)


@pytest.mark.asyncio
async def test_opus_mode_encodes_non_rendering_provider_bytes_without_touching_disk():
    """edge_tts-shaped engines (no render_wav(), MP3 bytes) must keep working in
    Opus mode without ever touching the artifact store -- render_audio() is the
    only seam now, and wav_bytes_to_pcm16's soundfile fallback decodes the
    non-WAV container in memory. Uses a genuine MP3 container (not a RIFF/WAVE
    one mislabeled as MP3): wave.open() succeeds on any RIFF bytes regardless of
    the declared media_type, so a real WAV double would never actually exercise
    the soundfile fallback this test is named for."""
    fake_mp3 = _tone_mp3(int(0.1 * OUT_SR), 220.0, OUT_SR)  # 100ms @ 24kHz
    assert fake_mp3[:4] != b"RIFF"  # sanity: genuinely not a WAV container
    provider = _NonRenderingTTS(fake_mp3)
    tts_service.providers["stub-opus-nodisk-nonrender-tts"] = provider

    before = set(artifact_store.base_dir.iterdir())
    _session, _events, audio_frames = await _drive_text_turn(
        _cfg(audio_out="opus", tts_engine="stub-opus-nodisk-nonrender-tts")
    )
    assert audio_frames  # opus packets were produced
    assert set(artifact_store.base_dir.iterdir()) == before
