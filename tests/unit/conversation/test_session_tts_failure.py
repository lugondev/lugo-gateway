"""When TTS fails mid-turn, the LLM text must still reach the client and the
failure must be reported -- instead of the whole turn unwinding to a generic
`error` with the assistant's words swallowed.

Regression guard for the audio-path bug where `response_text` was emitted only
*after* a successful synth, so a raising TTS provider lost the text entirely.
"""

import pytest
from app.schemas.stt import STTResult
from app.services.conversation.session import ConversationSession, SessionRuntimeConfig
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service

SR = 16000


class _StubSTT(STTProvider):
    name = "stub-ttsfail-stt"
    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chao", is_final=True)


class _BoomTTS(TTSProvider):
    """Every render_audio attempt raises -- simulates a misconfigured/down TTS engine."""
    name = "stub-ttsfail-tts"

    async def render_audio(self, payload) -> tuple[bytes, str]:
        raise RuntimeError("tts engine exploded")


class _FakeResponder:
    name = "stub-ttsfail-responder"

    async def reply(self, history: list[dict]) -> str:
        return "one two"

    async def reply_stream(self, history, registry=None, ctx=None, max_iters=3):
        yield "Cau tra loi mot."
        yield "Cau tra loi hai."


async def _fake_build_responder_ex(*args, **kwargs):
    return _FakeResponder()


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    stt_service.providers["stub-ttsfail-stt"] = _StubSTT()
    tts_service.providers["stub-ttsfail-tts"] = _BoomTTS()
    monkeypatch.setattr(
        "app.services.conversation.session.build_responder_ex", _fake_build_responder_ex
    )
    yield
    stt_service.providers.pop("stub-ttsfail-stt", None)
    tts_service.providers.pop("stub-ttsfail-tts", None)


def _cfg(**over):
    base = dict(
        session_id="s1", profile_name=None, stt_engine="stub-ttsfail-stt", language="vi",
        tts_engine="stub-ttsfail-tts", voice=None, ref_audio_path=None, ref_text=None,
        tts_instruct=None, tts_speed=None, tts_language=None, sample_rate=SR,
        output_sample_rate=24000, audio_codec="pcm16", want_audio=True, want_text=True,
        audio_out="wav", denoise=False, resume_sid=None,
    )
    base.update(over)
    return SessionRuntimeConfig(**base)


async def _drive_text_turn(cfg):
    events: list = []
    audio_pkts: list = []

    async def emit(name, **p):
        events.append((name, p))

    async def emit_audio(pkt):
        audio_pkts.append(pkt)

    sess = ConversationSession(cfg, emit, emit_audio)
    await sess.start()
    await sess.feed_text("hello")
    await sess.wait_current_turn()
    await sess.close()
    return sess, events, audio_pkts


async def test_tts_failure_still_emits_text_and_reports_error():
    sess, events, audio_pkts = await _drive_text_turn(_cfg())
    names = [n for n, _ in events]

    # 1. The LLM text is still shown -- both streamed sentences.
    texts = [p["text"] for n, p in events if n == "response_text"]
    assert texts == ["Cau tra loi mot.", "Cau tra loi hai."], texts

    # 2. The TTS failure is reported (not just swallowed into a generic error).
    assert "tts_error" in names, names

    # 3. The turn completes normally rather than aborting with a bare `error`.
    assert "turn_done" in names, names

    # 4. The assistant turn is still recorded in history (persistence not skipped).
    assistant = [m for m in sess.history if m.get("role") == "assistant" and m.get("content")]
    assert assistant, sess.history
    assert "Cau tra loi mot." in assistant[-1]["content"]


async def test_tts_failure_emits_no_audio():
    """No audio frames when synth fails -- the client falls back to text only."""
    _sess, _events, audio_pkts = await _drive_text_turn(_cfg())
    assert audio_pkts == []
