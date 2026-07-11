import asyncio

import pytest
from app.core.settings import settings
from app.schemas.stt import STTResult
from app.schemas.tts import TTSResult
from app.services.conversation.session import ConversationSession, SessionRuntimeConfig
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service

SR = 16000


class _StubSTT(STTProvider):
    name = "stub-core-stt"
    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chao", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-core-tts"
    async def synthesize(self, payload) -> TTSResult:
        return TTSResult(engine=self.name, sample_rate=24000,
                         audio_url="/artifacts/x.wav", duration_seconds=0.1, text=payload.text)


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    monkeypatch.setattr(settings, "conversation_llm_base_url", "")
    stt_service.providers["stub-core-stt"] = _StubSTT()
    tts_service.providers["stub-core-tts"] = _StubTTS()
    yield
    stt_service.providers.pop("stub-core-stt", None)
    tts_service.providers.pop("stub-core-tts", None)


def _cfg(**over):
    base = dict(
        session_id="s1", profile_name=None, stt_engine="stub-core-stt", language="vi",
        tts_engine="stub-core-tts", voice=None, ref_audio_path=None, ref_text=None,
        tts_instruct=None, tts_speed=None, tts_language=None, sample_rate=SR,
        output_sample_rate=24000, audio_codec="pcm16", want_audio=False, want_text=True,
        audio_out="url", denoise=False, resume_sid=None,
    )
    base.update(over)
    return SessionRuntimeConfig(**base)


class _RecordingSTT(STTProvider):
    name = "stub-record-stt"

    def __init__(self):
        self.seen_models: list[str | None] = []

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        self.seen_models.append(model)
        return STTResult(engine=self.name, text=f"heard:{model}", is_final=True)


async def test_two_sessions_transcribe_with_their_own_pinned_model():
    """Session A pins 'small', session B pins 'medium' on the same engine. Running
    a turn on each — even interleaved — must not let B's model affect A's turn."""
    provider = _RecordingSTT()
    stt_service.providers["stub-record-stt"] = provider
    try:
        events_a: list = []
        events_b: list = []

        async def emit_a(name, **p):
            events_a.append((name, p))

        async def emit_b(name, **p):
            events_b.append((name, p))

        async def noop_audio(pkt):
            pass

        sess_a = ConversationSession(
            _cfg(stt_engine="stub-record-stt", stt_model="small"), emit_a, noop_audio
        )
        sess_b = ConversationSession(
            _cfg(stt_engine="stub-record-stt", stt_model="medium"), emit_b, noop_audio
        )
        await sess_a.start()
        await sess_b.start()

        assert sess_a.stt_model_id == "small"
        assert sess_b.stt_model_id == "medium"

        # Drive an actual audio turn on each session (feed_text bypasses STT
        # entirely, so it wouldn't reach transcribe_bytes / prove model pinning).
        # Call _handle_turn directly -- the same entry point feed_audio's VAD
        # endpointer uses once it detects an endpoint -- so we deterministically
        # exercise the STT call without needing to fabricate VAD-triggering audio.
        pcm = b"\x00\x00" * 1600
        sess_a.current_turn = asyncio.create_task(sess_a._handle_turn(audio_pcm=pcm, speech_ms=300))
        await sess_a.wait_current_turn()
        sess_b.current_turn = asyncio.create_task(sess_b._handle_turn(audio_pcm=pcm, speech_ms=300))
        await sess_b.wait_current_turn()

        await sess_a.close()
        await sess_b.close()

        assert "small" in provider.seen_models
        assert "medium" in provider.seen_models
    finally:
        stt_service.providers.pop("stub-record-stt", None)


@pytest.mark.asyncio
async def test_text_turn_emits_transcript_and_reply():
    events = []
    async def emit(name, **p): events.append((name, p))
    async def emit_audio(pkt): events.append(("_audio", {"len": len(pkt)}))

    sess = ConversationSession(_cfg(), emit, emit_audio)
    await sess.start()
    await sess.feed_text("hello")
    await sess.wait_current_turn()
    await sess.close()

    names = [n for n, _ in events]
    assert "session_started" in names
    assert "user_transcript" in names
    assert "turn_done" in names


@pytest.mark.asyncio
async def test_session_start_pins_explicit_stt_model():
    """A cfg.stt_model override must be snapshotted onto the session as
    stt_model_id, not applied via a process-global mutation."""
    async def emit(name, **p): pass
    async def emit_audio(pkt): pass

    sess = ConversationSession(_cfg(stt_model="1.7b"), emit, emit_audio)
    await sess.start()
    await sess.close()

    assert sess.stt_model_id == "1.7b"


@pytest.mark.asyncio
async def test_session_start_skips_apply_when_no_model_set():
    async def emit(name, **p): pass
    async def emit_audio(pkt): pass

    sess = ConversationSession(_cfg(), emit, emit_audio)  # stt_model defaults to ""
    await sess.start()  # must not raise
    await sess.close()

    assert sess.stt_model_id is None  # stub-core-stt has no model-variant registry


@pytest.mark.asyncio
async def test_session_start_does_not_mutate_the_process_global_active_model():
    """Regression guard: session.start() must resolve+snapshot stt_model_id for
    this session only, and must NOT flip the process-global active model the
    way the old apply_stt_model() call used to -- that's exactly the cross-session
    interference bug this task fixes (see test_stt_model_param_isolation.py for
    the provider-level half of this guarantee)."""
    from app.services.stt.providers import qwen3_asr_provider as q

    async def emit(name, **p): pass
    async def emit_audio(pkt): pass

    original = q.get_active_qwen3_asr_model()
    sess = ConversationSession(_cfg(stt_engine="qwen3_asr", stt_model="1.7b"), emit, emit_audio)
    try:
        await sess.start()
        assert sess.stt_model_id == "1.7b"
        assert q.get_active_qwen3_asr_model() == original
    finally:
        await sess.close()
        q.set_active_qwen3_asr_model(None)
