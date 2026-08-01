"""Every turn that starts must emit `turn_done`, including the ones that fail.

`turn_done` is not cosmetic bookkeeping: it is the only event the transports
read as "the assistant has stopped". api/routes/lugo.py's emit() clears its
`speaking` flag there and, crucially, consumes `close_after_speaking` there --
so a turn that returned bare left a device that had already armed a hang-up
(idle watchdog, or the end_conversation tool) waiting for an event that was
never coming.

Two paths used to return without one: an over-quota pre-flight, and an STT
failure.
"""

import pytest
from app.core.audio import pcm16_to_wav_bytes
from app.schemas.stt import STTResult
from app.services.conversation.session import ConversationSession, SessionRuntimeConfig
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import RenderingTTSProvider
from app.services.tts.service import tts_service

SR = 16000


def _silence_wav(ms: int = 100, sr: int = 24000) -> bytes:
    n = int(sr * ms / 1000)
    return pcm16_to_wav_bytes(b"\x00\x00" * n, sample_rate=sr)


class _BrokenSTT(STTProvider):
    name = "stub-broken-stt"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        raise RuntimeError("model not loaded")


class _OkSTT(STTProvider):
    name = "stub-ok-stt"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chao", is_final=True)


class _StubTTS(RenderingTTSProvider):
    name = "stub-closeout-tts"

    async def _render_wav(self, payload) -> bytes:
        return _silence_wav()


@pytest.fixture(autouse=True)
def _stubs():
    stt_service.providers["stub-broken-stt"] = _BrokenSTT()
    stt_service.providers["stub-ok-stt"] = _OkSTT()
    tts_service.providers["stub-closeout-tts"] = _StubTTS()
    yield
    stt_service.providers.pop("stub-broken-stt", None)
    stt_service.providers.pop("stub-ok-stt", None)
    tts_service.providers.pop("stub-closeout-tts", None)


def _cfg(**over):
    base = dict(
        session_id="s-closeout", profile_name=None, stt_engine="stub-ok-stt",
        language="vi", tts_engine="stub-closeout-tts", voice=None,
        ref_audio_path=None, ref_text=None, tts_instruct=None, tts_speed=None,
        tts_language=None, sample_rate=SR, output_sample_rate=24000,
        audio_codec="pcm16", want_audio=False, want_text=True, audio_out="wav",
        denoise=False, resume_sid=None,
    )
    base.update(over)
    return SessionRuntimeConfig(**base)


async def _run(cfg) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []

    async def emit(event, **payload):
        events.append((event, payload))

    async def emit_audio(_packet):
        pass

    session = ConversationSession(cfg, emit, emit_audio)
    await session.start()
    events.clear()
    await session._run_turn(audio_pcm=b"\x00\x00" * SR, speech_ms=500.0)
    return events


@pytest.mark.asyncio
async def test_stt_failure_still_closes_the_turn():
    events = await _run(_cfg(stt_engine="stub-broken-stt"))
    names = [name for name, _ in events]

    assert "error" in names
    assert "turn_done" in names
    done = next(payload for name, payload in events if name == "turn_done")
    # Marked skipped so lugo's refreshes_idle() doesn't count a failure as
    # interaction and hold the idle countdown open.
    assert done["skipped"]


@pytest.mark.asyncio
async def test_over_quota_turn_still_closes_the_turn(monkeypatch):
    async def _blocked(**_kwargs):
        return True, "user quota exceeded: $1.00 / $1.00 (monthly)"

    monkeypatch.setattr(
        "app.services.conversation.session.llm_turn_quota_blocked", _blocked
    )
    events = await _run(_cfg())
    names = [name for name, _ in events]

    assert names.count("error") == 1
    assert "turn_done" in names
    assert next(p for n, p in events if n == "turn_done")["skipped"]
    # The turn was refused before any work: no transcript, no reply.
    assert "user_transcript" not in names
    assert "response_text" not in names
