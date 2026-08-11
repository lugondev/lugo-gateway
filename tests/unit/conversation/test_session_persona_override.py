"""cfg.persona_override lets a caller (e.g. the livehost plugin) supply its
own system prompt for one session, without needing edit access to a gateway
profile. See SessionRuntimeConfig.persona_override.
"""

import pytest

from app.core.audio import pcm16_to_wav_bytes
from app.schemas.stt import STTResult
from app.services.conversation.session import ConversationSession, SessionRuntimeConfig
from app.services.profiles.models import Profile
from app.services.profiles.store import profile_store
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service


class _StubSTT(STTProvider):
    name = "stub-persona-stt"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chao", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-persona-tts"

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
        session_id="persona-1",
        profile_name=None,
        stt_engine=_StubSTT.name,
        language="vi",
        tts_engine=_StubTTS.name,
        voice=None,
        ref_audio_path=None,
        ref_text=None,
        tts_instruct=None,
        tts_speed=None,
        tts_language=None,
        sample_rate=16000,
        output_sample_rate=24000,
        audio_codec="pcm16",
        want_audio=False,
        want_text=True,
        audio_out="wav",
        denoise=False,
        resume_sid=None,
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


async def test_persona_override_replaces_the_server_default():
    session = await _session(_cfg(persona_override="You are Lan, the channel owner."))
    assert "You are Lan, the channel owner." in session.base_system_prompt
    assert "helpful, concise voice assistant" not in session.base_system_prompt


async def test_persona_override_wins_over_the_profiles_own_prompt():
    profile_store.upsert(
        Profile(name="persona-test-profile", system_prompt="You are the profile's own persona.")
    )
    session = await _session(
        _cfg(
            profile_name="persona-test-profile", persona_override="You are Lan, the channel owner."
        )
    )
    assert "You are Lan, the channel owner." in session.base_system_prompt
    assert "profile's own persona" not in session.base_system_prompt


async def test_no_override_falls_back_to_the_profiles_own_prompt_as_before():
    profile_store.upsert(
        Profile(name="persona-test-profile-2", system_prompt="You are the profile's own persona.")
    )
    session = await _session(_cfg(profile_name="persona-test-profile-2"))
    assert "profile's own persona" in session.base_system_prompt


async def test_blank_override_is_treated_as_no_override():
    """An empty string from ?system_prompt= (query param present but blank)
    must not silently blank out the persona -- same "falsy means unset"
    contract profile.system_prompt already uses in resolve_llm_config."""
    profile_store.upsert(
        Profile(name="persona-test-profile-3", system_prompt="You are the profile's own persona.")
    )
    session = await _session(_cfg(profile_name="persona-test-profile-3", persona_override=""))
    assert "profile's own persona" in session.base_system_prompt
