"""Three loose ends in ConversationSession's lifecycle:

* the boot warm-up task was created with a bare ``asyncio.create_task`` and
  never retained, instead of going through the ``spawn_background`` helper
  (conversation/background.py) that exists because a task nobody holds a
  reference to can be garbage-collected mid-flight;
* ``close()`` cancelled the in-flight turn and then closed the responder
  without waiting, so the turn could still be unwinding through a responder
  that was already shut;
* leftover ``DEBUG_HANG`` instrumentation logged every synthesized sentence at
  INFO, writing private conversation content into the server log.
"""

import asyncio
import logging

import pytest

from app.core.audio import pcm16_to_wav_bytes
from app.schemas.stt import STTResult
from app.services.conversation import background as background_module
from app.services.conversation.session import ConversationSession, SessionRuntimeConfig
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service

SPOKEN_SECRET = "so tai khoan cua toi la 123456789"


class _StubSTT(STTProvider):
    name = "stub-teardown-stt"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text=SPOKEN_SECRET, is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-teardown-tts"

    async def render_audio(self, payload) -> tuple[bytes, str]:
        return pcm16_to_wav_bytes(b"\x00\x00" * 240, sample_rate=24000), "audio/wav"


@pytest.fixture(autouse=True)
def _stub_engines():
    stt_service.providers[_StubSTT.name] = _StubSTT()
    tts_service.providers[_StubTTS.name] = _StubTTS()
    yield
    stt_service.providers.pop(_StubSTT.name, None)
    tts_service.providers.pop(_StubTTS.name, None)


def _cfg(**overrides) -> SessionRuntimeConfig:
    base = dict(
        session_id="teardown-1", profile_name=None,
        stt_engine=_StubSTT.name, language=None, tts_engine=_StubTTS.name,
        voice=None, ref_audio_path=None, ref_text=None, tts_instruct=None,
        tts_speed=None, tts_language=None, sample_rate=16000,
        output_sample_rate=24000, audio_codec="pcm16",
        want_audio=False, want_text=True, audio_out="wav",
        denoise=False, resume_sid=None,
    )
    base.update(overrides)
    return SessionRuntimeConfig(**base)


async def _new_session(**overrides):
    async def emit(event, **payload):
        return None

    async def emit_audio(packet):
        return None

    return ConversationSession(_cfg(**overrides), emit, emit_audio)


async def test_the_warmup_task_is_retained_so_it_cannot_be_collected():
    # The retention set lives in conversation/background.py, next to the
    # shutdown drain that is the other half of the same contract; session.py
    # only re-exports the spawn helper under its old private name.
    background_module._background_tasks.clear()
    sess = await _new_session()

    await sess.start()

    assert background_module._background_tasks, (
        "start() spawned a fire-and-forget task with no strong reference"
    )
    await sess.close()


async def test_close_waits_for_the_cancelled_turn_before_shutting_the_responder():
    sess = await _new_session()
    await sess.start()

    unwound = asyncio.Event()

    async def _slow_turn():
        try:
            await asyncio.sleep(10)
        finally:
            unwound.set()

    sess.current_turn = asyncio.create_task(_slow_turn())
    await asyncio.sleep(0)  # let it reach the sleep

    await sess.close()

    assert sess.current_turn.done()
    assert unwound.is_set()


async def test_a_turn_does_not_log_what_was_said(caplog):
    # want_audio, so the turn goes through the prefetch/_synth pipeline where
    # the DEBUG_HANG lines lived -- the text-only branch never reaches them.
    sess = await _new_session(want_audio=True)
    await sess.start()
    caplog.clear()
    with caplog.at_level(logging.INFO):
        await sess.feed_text(SPOKEN_SECRET)
        await sess.wait_current_turn()
    await sess.close()

    assert SPOKEN_SECRET not in caplog.text
    assert "DEBUG_HANG" not in caplog.text
