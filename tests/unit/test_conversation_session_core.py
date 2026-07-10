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
    async def transcribe_bytes(self, audio_bytes, language=None) -> STTResult:
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
async def test_session_start_applies_profile_stt_model(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.services.conversation.session.apply_stt_model",
        lambda engine, model: calls.append((engine, model)),
    )

    async def emit(name, **p): pass
    async def emit_audio(pkt): pass

    sess = ConversationSession(_cfg(stt_model="1.7b"), emit, emit_audio)
    await sess.start()
    await sess.close()

    assert calls == [("stub-core-stt", "1.7b")]


@pytest.mark.asyncio
async def test_session_start_skips_apply_when_no_model_set():
    async def emit(name, **p): pass
    async def emit_audio(pkt): pass

    sess = ConversationSession(_cfg(), emit, emit_audio)  # stt_model defaults to ""
    await sess.start()  # must not raise
    await sess.close()
