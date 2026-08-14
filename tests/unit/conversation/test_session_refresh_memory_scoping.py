"""Regression test for the WS live-turn memory-retrieval scoping gap.

Task 6 wired user-scoping into the WS teardown `extract_and_upsert` call and
the HTTP memory route, but missed `_refresh_memory` -- the per-turn call that
injects retrieved memories into the live system prompt on every turn (see
session.py's `_handle_turn` -> `_run_turn` -> `_refresh_memory`). Because
`get_context` defaults `user_id=None` (-> the shared-device '' bucket
downstream), a logged-in WS user would get the wrong bucket's memories
injected into their live conversation, and facts extracted at teardown under
their real identity would never come back during the session.

The fix: `_refresh_memory` must pass `self.cfg.identity_user_id` through to
`memory_retriever.get_context`, exactly like the teardown
`extract_and_upsert` call already does.
"""

import pytest
from app.core.audio import pcm16_to_wav_bytes
from app.schemas.stt import STTResult
from app.services.conversation import session as session_module
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
    name = "stub-refresh-mem-stt"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chao", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-refresh-mem-tts"

    async def render_audio(self, payload) -> tuple[bytes, str]:
        return _silence_wav(), "audio/wav"


@pytest.fixture(autouse=True)
def _stubs():
    stt_service.providers["stub-refresh-mem-stt"] = _StubSTT()
    tts_service.providers["stub-refresh-mem-tts"] = _StubTTS()
    yield
    stt_service.providers.pop("stub-refresh-mem-stt", None)
    tts_service.providers.pop("stub-refresh-mem-tts", None)


def _cfg(**over):
    base = dict(
        session_id="s1", profile_name=None, stt_engine="stub-refresh-mem-stt", language="vi",
        tts_engine="stub-refresh-mem-tts", voice=None, ref_audio_path=None, ref_text=None,
        tts_instruct=None, tts_speed=None, tts_language=None, sample_rate=SR,
        output_sample_rate=24000, audio_codec="pcm16", want_audio=False, want_text=True,
        audio_out="wav", denoise=False, resume_sid=None,
    )
    base.update(over)
    return SessionRuntimeConfig(**base)


@pytest.mark.asyncio
async def test_refresh_memory_threads_session_identity_into_get_context(monkeypatch, tmp_path):
    """`_refresh_memory` (the per-turn live memory injection) must scope its
    `get_context` call to the authenticated WS caller's identity, not the
    default (None -> shared-device '' bucket).

    Note: `_refresh_memory` no-ops unless `self.responder` has a
    `system_prompt` attribute, which only `OpenAICompatResponder` has (the
    default `EchoResponder`, used when no LLM base_url is configured, does
    not). So this test configures a profile with an LLM base_url set --
    same pattern as test_conversation_session_core.py's
    test_session_started_reports_the_profiles_actual_llm_model -- to force
    build_responder_ex() to build an OpenAICompatResponder and actually
    exercise the memory-injection code path."""
    from app.services.profiles.models import LlmConfig, Profile
    from app.services.profiles.store import ProfileStore

    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    monkeypatch.setattr(session_module, "profile_store", fresh_profiles)
    # usable_profile_or_none now requires shared or owner match -- bind the
    # profile to the same identity_user_id the session below authenticates
    # as, matching Root Cause A of task-3b-brief.md (an ownerless profile is
    # no longer visible/usable to anyone).
    fresh_profiles.upsert(Profile(
        name="refresh-mem-profile",
        owner_id="user-xyz-123",
        llm=LlmConfig(base_url="https://api.example.com/v1", api_key="k", model="test-model"),
    ))

    seen_user_ids = []

    async def _spy_get_context(profile, query="", user_id=None):
        seen_user_ids.append(user_id)
        return ""

    monkeypatch.setattr(session_module.memory_retriever, "get_context", _spy_get_context)

    async def emit(name, **p):
        pass

    async def emit_audio(pkt):
        pass

    sess = ConversationSession(
        _cfg(profile_name="refresh-mem-profile", identity_user_id="user-xyz-123"), emit, emit_audio
    )
    await sess.start()
    assert hasattr(sess.responder, "system_prompt"), "expected an LLM responder for this test to be meaningful"
    try:
        await sess._refresh_memory("some query")
    finally:
        await sess.close()

    assert seen_user_ids == ["user-xyz-123"], (
        f"expected _refresh_memory to pass the session's identity_user_id through to "
        f"get_context, got {seen_user_ids!r}"
    )
