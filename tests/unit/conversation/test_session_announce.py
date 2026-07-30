"""The session side of spoken announcements: speak it, store it, or report why not.

Complements test_announce_line.py (which pins the prompt and the cleaning) and
test_session_rotate.py (which pins the rotation itself).
"""

import asyncio

import pytest

from app.core.audio import pcm16_to_wav_bytes
from app.schemas.stt import STTResult
from app.services.conversation.session import ConversationSession, SessionRuntimeConfig
from app.services.history.store import session_store
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service


class _StubSTT(STTProvider):
    name = "stub-announce-stt"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chao", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-announce-tts"

    def __init__(self):
        self.spoken: list[str] = []

    async def render_audio(self, payload) -> tuple[bytes, str]:
        self.spoken.append(payload.text)
        return pcm16_to_wav_bytes(b"\x00\x00" * 240, sample_rate=24000), "audio/wav"


class _BrokenTTS(TTSProvider):
    name = "stub-announce-tts-broken"

    async def render_audio(self, payload) -> tuple[bytes, str]:
        raise RuntimeError("voice model exploded")


class _StubResponder:
    name = "stub-announce-llm"

    def __init__(self, reply_text="Mình bắt đầu lại nha!"):
        self.reply_text = reply_text

    async def reply(self, history: list[dict]) -> str:
        return self.reply_text

    async def aclose(self) -> None:
        pass


class _BrokenResponder(_StubResponder):
    async def reply(self, history: list[dict]) -> str:
        raise RuntimeError("llm down")


@pytest.fixture
def stubs():
    tts = _StubTTS()
    stt_service.providers[_StubSTT.name] = _StubSTT()
    tts_service.providers[_StubTTS.name] = tts
    tts_service.providers[_BrokenTTS.name] = _BrokenTTS()
    yield tts
    stt_service.providers.pop(_StubSTT.name, None)
    tts_service.providers.pop(_StubTTS.name, None)
    tts_service.providers.pop(_BrokenTTS.name, None)


def _cfg(**over) -> SessionRuntimeConfig:
    base = dict(
        session_id="announce-1", profile_name=None, stt_engine=_StubSTT.name, language="vi",
        tts_engine=_StubTTS.name, voice=None, ref_audio_path=None, ref_text=None,
        tts_instruct=None, tts_speed=None, tts_language=None, sample_rate=16000,
        output_sample_rate=24000, audio_codec="pcm16", want_audio=True, want_text=True,
        audio_out="wav", denoise=False, resume_sid=None,
    )
    base.update(over)
    return SessionRuntimeConfig(**base)


async def _session(cfg, responder=None):
    events: list[tuple[str, dict]] = []

    async def emit(name, **payload):
        events.append((name, payload))

    async def emit_audio(_packet):
        pass

    session = ConversationSession(cfg, emit, emit_audio)
    await session.start()
    session.responder = responder or _StubResponder()
    events.clear()
    return session, events


def _names(events):
    return [n for n, _ in events]


@pytest.mark.asyncio
async def test_the_announcement_llm_call_is_metered(stubs, monkeypatch):
    """A line nobody asked for still costs tokens. It is a paid call site (see
    tests/unit/test_paid_call_site_inventory.py), so it writes a usage row."""
    rows: list[dict] = []

    async def _record_usage(**kwargs):
        rows.append(kwargs)

    monkeypatch.setattr("app.services.conversation.turn_usage.record_usage", _record_usage)
    responder = _StubResponder()
    responder.last_usage = {"prompt_tokens": 90, "completion_tokens": 11}
    session, _events = await _session(_cfg(session_id="announce-metered"), responder=responder)

    await session.announce("new_session")

    llm_rows = [r for r in rows if r.get("kind") == "llm"]
    assert llm_rows, f"the announcement's LLM call went unmetered: {rows}"
    assert llm_rows[0]["native_amount"] == 101


@pytest.mark.asyncio
async def test_tokens_spent_on_an_unusable_answer_are_still_metered(stubs, monkeypatch):
    """The call succeeded and the answer was empty: the tokens are gone either way,
    so the row is written even though nothing gets spoken."""
    rows: list[dict] = []

    async def _record_usage(**kwargs):
        rows.append(kwargs)

    monkeypatch.setattr("app.services.conversation.turn_usage.record_usage", _record_usage)
    responder = _StubResponder(reply_text="   ")   # generate_line raises on this
    responder.last_usage = {"prompt_tokens": 40, "completion_tokens": 0}
    session, events = await _session(_cfg(session_id="announce-metered-empty"),
                                     responder=responder)

    await session.announce("new_session")

    assert [r for r in rows if r.get("kind") == "llm"], f"spend went unrecorded: {rows}"
    assert stubs.spoken == []
    assert any(n == "error" for n, _ in events)


@pytest.mark.asyncio
async def test_over_quota_skips_silently_without_calling_the_llm(stubs, monkeypatch):
    """Server-initiated work over quota is skipped, not refused: no LLM call, no
    speech, and no error on the user's display for something they never asked for."""
    called = False

    async def _blocked(**kwargs):
        return True, "over quota"

    monkeypatch.setattr(
        "app.services.conversation.session.llm_turn_quota_blocked", _blocked
    )

    class _CountingResponder(_StubResponder):
        async def reply(self, history):
            nonlocal called
            called = True
            return "should never be asked"

    session, events = await _session(_cfg(session_id="announce-quota"),
                                     responder=_CountingResponder())

    await session.announce("idle_goodbye")

    assert not called
    assert stubs.spoken == []
    assert "error" not in _names(events)


@pytest.mark.asyncio
async def test_announce_speaks_persists_and_remembers(stubs):
    session, events = await _session(_cfg(session_id="announce-speak"))

    await session.announce("new_session")

    assert stubs.spoken == ["Mình bắt đầu lại nha!"]
    # In history, so the model knows it already greeted and doesn't greet again.
    assert session.history[-1] == {"role": "assistant", "content": "Mình bắt đầu lại nha!"}
    stored = await session_store.get_messages("announce-speak")
    assert [m["content"] for m in stored] == ["Mình bắt đầu lại nha!"]
    assert "error" not in _names(events)


@pytest.mark.asyncio
async def test_a_dead_llm_is_reported_not_papered_over(stubs):
    """Silence with no explanation is the failure mode being avoided: the device
    renders `error` on its panel, so "it went quiet" becomes "the LLM broke"."""
    session, events = await _session(_cfg(session_id="announce-llm-fail"),
                                     responder=_BrokenResponder())

    await session.announce("idle_goodbye")

    assert stubs.spoken == []
    errors = [p["message"] for n, p in events if n == "error"]
    assert errors and "llm" in errors[0].lower()
    assert session.history == []
    assert await session_store.get_messages("announce-llm-fail") == []


@pytest.mark.asyncio
async def test_a_dead_tts_is_reported_as_tts(stubs):
    session, events = await _session(_cfg(session_id="announce-tts-fail",
                                          tts_engine=_BrokenTTS.name))

    await session.announce("new_session")

    errors = [p["message"] for n, p in events if n == "error"]
    assert errors and "tts" in errors[0].lower()


@pytest.mark.asyncio
async def test_a_text_only_session_announces_nothing(stubs):
    """The feature is a spoken line. With no audio downlink there is nothing to say,
    and persisting a line nobody hears would just litter History."""
    session, events = await _session(_cfg(session_id="announce-textonly", want_audio=False))

    await session.announce("new_session")

    assert stubs.spoken == []
    assert session.history == []
    assert "error" not in _names(events)


@pytest.mark.asyncio
async def test_rotating_from_a_button_announces_the_new_conversation(stubs):
    session, events = await _session(_cfg(session_id="announce-rotate"))
    session.turn = 1   # a conversation worth ending exists

    await session.request_rotate("client")

    assert "session_rotated" in _names(events)
    assert stubs.spoken == ["Mình bắt đầu lại nha!"]


@pytest.mark.asyncio
async def test_rotating_an_empty_conversation_announces_nothing(stubs):
    """An empty session rotates to itself -- nothing ended, so there is nothing to
    announce, and pressing the button on a silent device costs no LLM call."""
    session, events = await _session(_cfg(session_id="announce-rotate-empty"))

    await session.request_rotate("client")

    assert "session_rotated" in _names(events)
    assert stubs.spoken == []


@pytest.mark.asyncio
async def test_rotating_from_a_voice_tool_stays_quiet(stubs):
    """The deferred path IS the voice path: the model confirmed it inside the turn
    that asked. A second confirmation would be the device saying it twice."""
    session, events = await _session(_cfg(session_id="announce-rotate-voice"))
    session.turn = 1
    session.current_turn = asyncio.create_task(asyncio.sleep(0.02))

    await session.request_rotate("client")
    await session.current_turn
    for _ in range(20):
        if "session_rotated" in _names(events):
            break
        await asyncio.sleep(0.01)

    assert "session_rotated" in _names(events)
    assert stubs.spoken == []
