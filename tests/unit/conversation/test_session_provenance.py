"""Which client a conversation belongs to, and what "continue" means.

A session row used to record `user_id` and nothing about where it came from, so a
speaker's conversations and their owner's browser conversations were the same kind
of row. "Resume the latest" then meant "the latest of ANY client": the browser
adopted whatever the speaker had just been saying, and the speaker -- which
remembers no id at all -- opened a new conversation on every single wake.

Guessing is now scoped to the client. Asking (an explicit session id, chosen from
History) still is not: that is a person deciding to carry one conversation to
another screen.
"""

import pytest

from app.core.audio import pcm16_to_wav_bytes
from app.schemas.stt import STTResult
from app.services.conversation.session import ConversationSession, SessionRuntimeConfig
from app.services.history.store import session_store
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.system_config import system_config_store
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service


class _StubSTT(STTProvider):
    name = "stub-prov-stt"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chao", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-prov-tts"

    async def render_audio(self, payload) -> tuple[bytes, str]:
        return pcm16_to_wav_bytes(b"\x00\x00" * 240, sample_rate=24000), "audio/wav"


@pytest.fixture(autouse=True)
def _stubs():
    stt_service.providers[_StubSTT.name] = _StubSTT()
    tts_service.providers[_StubTTS.name] = _StubTTS()
    yield
    stt_service.providers.pop(_StubSTT.name, None)
    tts_service.providers.pop(_StubTTS.name, None)


def _cfg(**over) -> SessionRuntimeConfig:
    base = dict(
        session_id="prov-1", profile_name="dev", stt_engine=_StubSTT.name, language="vi",
        tts_engine=_StubTTS.name, voice=None, ref_audio_path=None, ref_text=None,
        tts_instruct=None, tts_speed=None, tts_language=None, sample_rate=16000,
        output_sample_rate=24000, audio_codec="pcm16", want_audio=False, want_text=True,
        audio_out="wav", denoise=False, resume_sid=None,
    )
    base.update(over)
    return SessionRuntimeConfig(**base)


async def _session(cfg) -> ConversationSession:
    async def emit(name, **payload):
        pass

    async def emit_audio(_packet):
        pass

    session = ConversationSession(cfg, emit, emit_audio)
    await session.start()
    return session


@pytest.mark.asyncio
async def test_a_connection_nobody_speaks_into_leaves_nothing_behind():
    """Lazy creation, the way chat products do it: a wake with no words, a page
    load, a health probe -- none of them is a conversation."""
    session = await _session(_cfg(session_id="prov-empty"))
    await session.close()

    assert await session_store.get("prov-empty") is None


@pytest.mark.asyncio
async def test_the_first_message_creates_the_conversation_with_its_provenance():
    session = await _session(_cfg(session_id="prov-first", source="device", client_id="dev-7"))
    await session._persist("user", "chào bạn")

    row = await session_store.get("prov-first")
    assert row is not None
    assert (row["source"], row["client_id"]) == ("device", "dev-7")


@pytest.mark.asyncio
async def test_a_device_reconnecting_continues_its_own_conversation():
    """The speaker sends no session id -- it never remembers one -- so the server
    resolves the thread from who is connecting."""
    first = await _session(_cfg(session_id="prov-dev-a", source="device", client_id="dev-9"))
    await first._persist("user", "mình thích ăn phở")
    await first.close()

    # A fresh connection, brand-new id, nothing asked for.
    second = await _session(_cfg(session_id="prov-dev-b", source="device", client_id="dev-9"))

    assert second.cfg.session_id == "prov-dev-a", "it started a new conversation instead"
    assert [m["content"] for m in second.history] == ["mình thích ăn phở"]
    row = await session_store.get("prov-dev-a")
    assert row["ended_at"] is None, "the resumed conversation still reads as ended"


@pytest.mark.asyncio
async def test_the_browser_does_not_adopt_the_speakers_conversation():
    """The bug this whole thing exists for: same person, two clients, one thread."""
    speaker = await _session(_cfg(session_id="prov-mix-device", source="device", client_id="dev-3"))
    await speaker._persist("user", "chuyện của cái loa")
    await speaker.close()

    browser = await _session(_cfg(session_id="prov-mix-web", source="web", client_id="user-3"))

    assert browser.cfg.session_id == "prov-mix-web"
    assert browser.history == []


@pytest.mark.asyncio
async def test_an_explicit_session_id_still_wins():
    """Continuing a conversation from History is a person's decision, and it may
    cross clients -- ownership is what guards that, not provenance."""
    await session_store.create("prov-explicit", source="device", client_id="dev-5")
    await session_store.append_message("prov-explicit", 1, "user", "chuyện cũ")
    await session_store.create("prov-newer", source="device", client_id="dev-5")
    await session_store.append_message("prov-newer", 1, "user", "chuyện mới")

    session = await _session(
        _cfg(session_id="prov-explicit", source="device", client_id="dev-5",
             resume_sid="prov-explicit")
    )

    assert [m["content"] for m in session.history] == ["chuyện cũ"]


@pytest.mark.asyncio
async def test_rows_without_provenance_are_never_resumed_into():
    """Everything written before these columns existed. Old data must not be
    guessed into somebody's current thread."""
    await session_store.create("prov-legacy")          # no source/client_id
    await session_store.append_message("prov-legacy", 1, "user", "dữ liệu cũ")

    session = await _session(_cfg(session_id="prov-fresh", source="device", client_id="dev-legacy"))

    assert session.cfg.session_id == "prov-fresh"
    assert session.history == []


@pytest.mark.asyncio
async def test_the_prompt_keeps_only_the_last_messages(monkeypatch):
    """The thread is unbounded (a client resumes it forever); the prompt is not."""
    _real_get = system_config_store.get
    monkeypatch.setattr(
        system_config_store, "get",
        lambda: _real_get().model_copy(update={
            "conversation": _real_get().conversation.model_copy(
                update={"conversation_history_max_messages": 4}
            )
        }),
    )
    await session_store.create("prov-long", source="device", client_id="dev-long")
    for i in range(10):
        await session_store.append_message("prov-long", i, "user", f"message-{i}")

    session = await _session(_cfg(session_id="prov-x", source="device", client_id="dev-long"))

    assert len(session.history) == 4
    assert session.history[-1]["content"] == "message-9"
    # The transcript itself is untouched -- History still shows everything.
    assert len(await session_store.get_messages("prov-long")) == 10


@pytest.mark.asyncio
async def test_the_text_input_path_also_keeps_only_the_last_messages(monkeypatch):
    """The audio path capped its history after every append; the text path did
    not, so a text-driven session replayed an ever-growing prompt to the LLM --
    exactly what _tail() exists to prevent, just on the other branch. announce()
    had the same omission.
    """
    _real_get = system_config_store.get
    monkeypatch.setattr(
        system_config_store, "get",
        lambda: _real_get().model_copy(update={
            "conversation": _real_get().conversation.model_copy(
                update={"conversation_history_max_messages": 4}
            )
        }),
    )
    session = await _session(_cfg(session_id="prov-text-cap"))

    for i in range(6):
        await session._run_turn(text_input=f"cau hoi {i}")

    assert len(session.history) == 4
    assert session.history[-2]["content"] == "cau hoi 5"
    # 6 turns x 2 messages, all still on the record.
    assert len(await session_store.get_messages("prov-text-cap")) == 12
