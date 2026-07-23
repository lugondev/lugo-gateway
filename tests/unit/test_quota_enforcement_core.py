"""Task 5: ConversationSession must abort a turn with a notice when over quota.

Approach: reuse the ConversationSession harness from
tests/unit/test_session_usage_metering.py (stubbed STT/TTS providers + a
profile with no LLM base_url so build_responder_ex falls back to the
built-in EchoResponder -- no real HTTP call needed). `_tmp_db`
(tests/conftest.py, autouse) points the DB engine at a fresh per-test sqlite
file, so we seed a real over-limit GLOBAL-scope quota via quota_store.create
+ a UsageEvent row (same pattern as tests/unit/test_quota_gate.py), then
drive ONE turn through `_handle_turn` and assert:

  (a) the turn was aborted before STT ran -- the stub STT provider's
      transcribe_bytes was never called, and no "user_transcript" /
      "response_text" / "turn_done" event was emitted; and
  (b) an "error" event (the session's existing turn-failure notice
      mechanism -- see session.py's STT-failure branch) was emitted, whose
      message names the quota.

A control test seeds no quota at all and asserts the turn runs normally
(STT is invoked, an assistant reply is produced) -- the existing metering
test already covers this in more detail, so this is a quick smoke check
scoped to "the gate doesn't block the happy path".
"""

import uuid

from app.schemas.stt import STTResult
from app.schemas.tts import TTSResult
from app.services.conversation.session import ConversationSession, SessionRuntimeConfig
from app.services.db.engine import db_session
from app.services.db.models import UsageEvent
from app.services.profiles.models import LlmConfig, Profile
from app.services.profiles.store import ProfileStore
from app.services.quota.store import quota_store
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service

SR = 16000


class _StubSTT(STTProvider):
    name = "stub-quota-stt"

    def __init__(self) -> None:
        self.calls = 0

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        self.calls += 1
        return STTResult(engine=self.name, text="xin chao", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-quota-tts"

    async def synthesize(self, payload) -> TTSResult:
        return TTSResult(engine=self.name, sample_rate=24000,
                         audio_url="/artifacts/x.wav", duration_seconds=0.1, text=payload.text)


def _cfg(**over):
    base = dict(
        session_id="s1", profile_name="quota-profile", stt_engine="stub-quota-stt", language="vi",
        tts_engine="stub-quota-tts", voice=None, ref_audio_path=None, ref_text=None,
        tts_instruct=None, tts_speed=None, tts_language=None, sample_rate=SR,
        output_sample_rate=24000, audio_codec="pcm16", want_audio=True, want_text=True,
        audio_out="url", denoise=False, resume_sid=None, identity_user_id="user-quota",
    )
    base.update(over)
    return SessionRuntimeConfig(**base)


async def _add_cost(user_id: str, provider_id: str, cost: float) -> None:
    async with db_session() as s:
        s.add(UsageEvent(
            id=str(uuid.uuid4()), user_id=user_id, profile_id="", provider_id=provider_id,
            kind="llm", engine="e", model_id="m", unit="tokens", native_amount=1, cost_usd=cost,
        ))
        await s.commit()


async def _make_session(monkeypatch, tmp_path, stt_provider, tts_provider):
    stt_service.providers["stub-quota-stt"] = stt_provider
    tts_service.providers["stub-quota-tts"] = tts_provider

    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    monkeypatch.setattr("app.services.conversation.session.profile_store", fresh_profiles)
    fresh_profiles.upsert(Profile(
        name="quota-profile",
        llm=LlmConfig(base_url="", api_key="", model="echo-model", engine="echo-engine"),
    ))

    events: list = []

    async def emit(name, **p):
        events.append((name, p))

    async def emit_audio(pkt):
        pass

    sess = ConversationSession(_cfg(), emit, emit_audio)
    await sess.start()
    return sess, events


async def test_turn_aborted_with_notice_when_over_quota(monkeypatch, tmp_path):
    # Global-scope quota, already over limit for anyone.
    await quota_store.create(scope="global", scope_id="", limit_usd=1.0, period="total")
    await _add_cost("someone-else", "", 5.0)

    stub_stt = _StubSTT()
    stub_tts = _StubTTS()
    try:
        sess, events = await _make_session(monkeypatch, tmp_path, stub_stt, stub_tts)

        pcm = b"\x00\x00" * 1600  # 100ms of 16kHz silence
        await sess._handle_turn(audio_pcm=pcm, speech_ms=100.0)
        await sess.close()

        # (a) the turn was aborted before STT ran.
        assert stub_stt.calls == 0
        names = [n for n, _ in events]
        assert "user_transcript" not in names
        assert "response_text" not in names
        assert "turn_done" not in names

        # (b) a notice/error event was emitted, naming the quota.
        error_events = [p for n, p in events if n == "error"]
        assert len(error_events) == 1
        assert "quota" in error_events[0]["message"].lower()
    finally:
        stt_service.providers.pop("stub-quota-stt", None)
        tts_service.providers.pop("stub-quota-tts", None)


async def test_turn_runs_normally_when_under_quota(monkeypatch, tmp_path):
    # No quota rows at all -> quota_gate finds nothing to enforce.
    stub_stt = _StubSTT()
    stub_tts = _StubTTS()
    try:
        sess, events = await _make_session(monkeypatch, tmp_path, stub_stt, stub_tts)

        pcm = b"\x00\x00" * 1600
        await sess._handle_turn(audio_pcm=pcm, speech_ms=100.0)
        await sess.close()

        assert stub_stt.calls == 1
        names = [n for n, _ in events]
        assert "user_transcript" in names
        assert "turn_done" in names
        assert "error" not in names
    finally:
        stt_service.providers.pop("stub-quota-stt", None)
        tts_service.providers.pop("stub-quota-tts", None)
