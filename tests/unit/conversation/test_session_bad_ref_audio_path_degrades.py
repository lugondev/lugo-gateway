"""A stored ref_audio_path that fails the artifacts-dir containment check
(TTSRequest's field_validator, schemas/tts.py -- 2026-07-28-critical-authz-fixes
task 5) must degrade the turn to `tts_error`, exactly like any other TTS
failure, instead of raising OUTSIDE _synth's try/except and unwinding the
whole turn -- which would swallow the LLM text that was already generated.

TtsProfile.ref_audio_path now rejects a bad path at SAVE time too (task-6
round-1 I2), so this can no longer happen via POST/PUT /v1/tts/profiles. But
SessionRuntimeConfig.ref_audio_path (a plain dataclass field, resolved from
whatever a TtsProfile happened to carry -- see api/routes/conversation.py's
WS handler) is not itself validated, so this is regression coverage for the
defense-in-depth fix in session.py's _synth (and speak()): the TTSRequest(...)
construction has to sit INSIDE the guarding try, not before it, so any future
source of a bad value here (a legacy row, a different resolution path, a
different validation failure entirely) still degrades rather than erroring
out the turn. Mirrors tests/unit/conversation/test_session_tts_failure.py's harness."""

import pytest
from app.core.audio import pcm16_to_wav_bytes
from app.schemas.stt import STTResult
from app.services.conversation.session import ConversationSession, SessionRuntimeConfig
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service

SR = 16000


def _silence_wav(ms: int = 100, sr: int = 24000) -> bytes:
    n = int(sr * ms / 1000)
    return pcm16_to_wav_bytes(b"\x00\x00" * n, sample_rate=sr)


class _StubSTT(STTProvider):
    name = "stub-badref-stt"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chao", is_final=True)


class _WouldSucceedTTS(TTSProvider):
    """Never actually reached: TTSRequest(...) construction fails first (bad
    ref_audio_path), before this provider's render_audio() is ever called. If
    this WERE called, it would succeed -- proving any observed failure is
    from validation, not from the (working) provider."""
    name = "stub-badref-tts"

    async def render_audio(self, payload) -> tuple[bytes, str]:
        return _silence_wav(), "audio/wav"


class _FakeResponder:
    name = "stub-badref-responder"

    async def reply(self, history: list[dict]) -> str:
        return "one two"

    async def reply_stream(self, history, registry=None, ctx=None, max_iters=3):
        yield "Cau tra loi mot."
        yield "Cau tra loi hai."


async def _fake_build_responder_ex(*args, **kwargs):
    return _FakeResponder()


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    stt_service.providers["stub-badref-stt"] = _StubSTT()
    tts_service.providers["stub-badref-tts"] = _WouldSucceedTTS()
    monkeypatch.setattr(
        "app.services.conversation.session.build_responder_ex", _fake_build_responder_ex
    )
    yield
    stt_service.providers.pop("stub-badref-stt", None)
    tts_service.providers.pop("stub-badref-tts", None)


def _cfg(**over):
    base = dict(
        session_id="s1", profile_name=None, stt_engine="stub-badref-stt", language="vi",
        tts_engine="stub-badref-tts", voice=None,
        # Outside the artifacts dir -- exactly what TTSRequest's field_validator
        # (schemas/tts.py) rejects. Simulates a stored value that reached this
        # far without going through TtsProfile's own (now-added) save-time check.
        ref_audio_path="/etc/passwd", ref_text=None,
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


async def test_bad_ref_audio_path_still_emits_text_and_degrades_to_tts_error():
    sess, events, audio_pkts = await _drive_text_turn(_cfg())
    names = [n for n, _ in events]

    # 1. The LLM text is still shown -- both streamed sentences, not swallowed.
    texts = [p["text"] for n, p in events if n == "response_text"]
    assert texts == ["Cau tra loi mot.", "Cau tra loi hai."], texts

    # 2. The bad-path ValidationError is reported as a tts_error, with the
    # containment message, not silently dropped or surfaced as a generic error.
    tts_errors = [p for n, p in events if n == "tts_error"]
    assert tts_errors, names
    assert "ref_audio_path" in tts_errors[0]["message"]
    assert "artifacts directory" in tts_errors[0]["message"]

    # 3. The turn completes normally -- no bare `error` event, no unwind.
    assert "turn_done" in names, names
    assert "error" not in names, names

    # 4. No audio was produced (construction never even reached the provider).
    assert audio_pkts == []

    # 5. The assistant turn is still recorded in history.
    assistant = [m for m in sess.history if m.get("role") == "assistant" and m.get("content")]
    assert assistant, sess.history
    assert "Cau tra loi mot." in assistant[-1]["content"]
