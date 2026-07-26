"""Task 5: ConversationSession must meter STT/LLM/TTS usage per turn.

Approach: drive a REAL turn (audio path) through ConversationSession with
stubbed STT/TTS providers and a profile whose LLM config has no base_url
(so build_responder_ex falls back to the built-in EchoResponder -- no real
HTTP call needed) but DOES carry an engine/model, so the LLM record_usage
call has something real to attribute cost to. `_tmp_db` (tests/conftest.py,
autouse) points the DB engine at a fresh per-test sqlite file, so we assert
real rows in `usage_events` rather than a record_usage spy -- the brief
prefers this when a full turn can be driven without genuine network I/O,
which is the case here (STT/TTS stubs + EchoResponder are all in-process).
"""

from sqlalchemy import select

from app.schemas.stt import STTResult
from app.schemas.tts import TTSResult
from app.services.conversation.session import ConversationSession, SessionRuntimeConfig
from app.services.db.engine import db_session
from app.services.db.models import UsageEvent
from app.services.profiles.models import LlmConfig, Profile
from app.services.profiles.store import ProfileStore
from app.services.stt.base import STTProvider
from app.services.system_config import system_config_store
from app.services.tts.base import TTSProvider
from app.services.stt.service import stt_service
from app.services.tts.service import tts_service

SR = 16000


class _StubSTT(STTProvider):
    name = "stub-meter-stt"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chao", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-meter-tts"

    async def synthesize(self, payload) -> TTSResult:
        return TTSResult(engine=self.name, sample_rate=24000,
                         audio_url="/artifacts/x.wav", duration_seconds=0.1, text=payload.text)


def _cfg(**over):
    base = dict(
        session_id="s1", profile_name="metered-profile", stt_engine="stub-meter-stt", language="vi",
        tts_engine="stub-meter-tts", voice=None, ref_audio_path=None, ref_text=None,
        tts_instruct=None, tts_speed=None, tts_language=None, sample_rate=SR,
        output_sample_rate=24000, audio_codec="pcm16", want_audio=True, want_text=True,
        audio_out="url", denoise=False, resume_sid=None, identity_user_id="user-42",
    )
    base.update(over)
    return SessionRuntimeConfig(**base)


async def _rows() -> list[UsageEvent]:
    async with db_session() as s:
        return list((await s.execute(select(UsageEvent))).scalars().all())


async def test_audio_turn_records_stt_llm_tts_usage(monkeypatch, tmp_path):
    stt_service.providers["stub-meter-stt"] = _StubSTT()
    tts_service.providers["stub-meter-tts"] = _StubTTS()

    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    monkeypatch.setattr("app.services.conversation.session.profile_store", fresh_profiles)
    # No base_url -> build_responder_ex falls back to EchoResponder (no real
    # HTTP call), but engine/model are still set so the LLM usage row has real
    # attribution values (record_usage reads profile.llm.engine/.model, not
    # the responder).
    fresh_profiles.upsert(Profile(
        name="metered-profile",
        llm=LlmConfig(base_url="", api_key="", model="echo-model", engine="echo-engine"),
    ))

    try:
        events: list = []

        async def emit(name, **p):
            events.append((name, p))

        async def emit_audio(pkt):
            pass

        sess = ConversationSession(_cfg(), emit, emit_audio)
        await sess.start()
        assert sess.responder.name == "echo"  # sanity: no real LLM I/O happened

        pcm = b"\x00\x00" * 1600  # 100ms of 16kHz silence
        await sess._handle_turn(audio_pcm=pcm, speech_ms=100.0)
        await sess.close()

        rows = await _rows()
        by_kind = {r.kind: r for r in rows}
        assert set(by_kind) == {"stt", "llm", "tts"}

        stt = by_kind["stt"]
        assert stt.engine == "stub-meter-stt"
        assert stt.unit == "seconds"
        assert stt.native_amount > 0
        assert stt.user_id == "user-42"
        assert stt.profile_id == "metered-profile"

        llm = by_kind["llm"]
        assert llm.engine == "echo-engine"
        assert llm.model_id == "echo-model"
        assert llm.unit == "tokens"
        assert llm.user_id == "user-42"
        assert llm.profile_id == "metered-profile"

        tts = by_kind["tts"]
        assert tts.engine == "stub-meter-tts"
        assert tts.unit == "chars"
        assert tts.native_amount > 0
        assert tts.user_id == "user-42"
        assert tts.profile_id == "metered-profile"
    finally:
        stt_service.providers.pop("stub-meter-stt", None)
        tts_service.providers.pop("stub-meter-tts", None)


async def test_metering_failure_never_breaks_the_turn(monkeypatch, tmp_path):
    """record_usage already swallows its own errors, but this guards the arg-
    building side: even if record_usage itself raises (simulating some future
    regression there), the turn must still complete and emit turn_done."""
    stt_service.providers["stub-meter-stt"] = _StubSTT()
    tts_service.providers["stub-meter-tts"] = _StubTTS()

    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    monkeypatch.setattr("app.services.conversation.session.profile_store", fresh_profiles)
    fresh_profiles.upsert(Profile(
        name="metered-profile",
        llm=LlmConfig(base_url="", api_key="", model="echo-model", engine="echo-engine"),
    ))

    async def boom(*a, **k):
        raise RuntimeError("db exploded")

    monkeypatch.setattr("app.services.conversation.session.record_usage", boom)

    try:
        events: list = []

        async def emit(name, **p):
            events.append((name, p))

        async def emit_audio(pkt):
            pass

        sess = ConversationSession(_cfg(), emit, emit_audio)
        await sess.start()

        pcm = b"\x00\x00" * 1600
        await sess._handle_turn(audio_pcm=pcm, speech_ms=100.0)
        await sess.close()

        names = [n for n, _ in events]
        assert "turn_done" in names
        assert "error" not in names
    finally:
        stt_service.providers.pop("stub-meter-stt", None)
        tts_service.providers.pop("stub-meter-tts", None)


class _StubFastSTT(STTProvider):
    """A second STT engine, distinct from the session's pinned engine, used
    to exercise the fast-path engine switch in `_handle_turn`."""

    name = "stub-meter-stt-fast"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="di", is_final=True)


async def test_fast_path_stt_switch_never_pairs_new_engine_with_old_pinned_model(monkeypatch, tmp_path):
    """Regression for the review finding: when `conversation_fast_stt_engine`
    routes a short utterance to a different engine than the session's
    configured `stt_engine`, the session's pinned `stt_model_id` (resolved for
    the ORIGINAL engine) must not be attributed to the NEW engine in the
    recorded usage event -- that (engine, model) pair was never actually used
    together, and `record_usage` looks it up as a single registry key. The
    recorded model_id must come from the same source as the engine actually
    used (turn_model), never fall back to self.stt_model_id when the engine
    was switched.
    """
    stt_service.providers["stub-meter-stt"] = _StubSTT()
    stt_service.providers["stub-meter-stt-fast"] = _StubFastSTT()
    tts_service.providers["stub-meter-tts"] = _StubTTS()

    # Force the fast path: any speech_ms <= max_ms routes to the fast engine.
    real_get = system_config_store.get

    def _get_with_fast_stt():
        cfg = real_get()
        return cfg.model_copy(update={
            "conversation": cfg.conversation.model_copy(update={
                "conversation_fast_stt_engine": "stub-meter-stt-fast",
                "conversation_fast_stt_max_ms": 1000,
            })
        })

    monkeypatch.setattr(
        "app.services.conversation.session.system_config_store.get", _get_with_fast_stt
    )

    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    monkeypatch.setattr("app.services.conversation.session.profile_store", fresh_profiles)
    fresh_profiles.upsert(Profile(
        name="metered-profile",
        llm=LlmConfig(base_url="", api_key="", model="echo-model", engine="echo-engine"),
    ))

    try:
        events: list = []

        async def emit(name, **p):
            events.append((name, p))

        async def emit_audio(pkt):
            pass

        # Pin an explicit model on the session's (original) STT engine, so a
        # wrong fallback to it would be visibly wrong once the engine switches.
        cfg = _cfg(stt_model="pinned-for-original-engine")
        sess = ConversationSession(cfg, emit, emit_audio)
        await sess.start()
        assert sess.stt_model_id == "pinned-for-original-engine"

        pcm = b"\x00\x00" * 1600  # 100ms of 16kHz silence -> under the 1000ms fast-path cap
        await sess._handle_turn(audio_pcm=pcm, speech_ms=100.0)
        await sess.close()

        rows = await _rows()
        stt = next(r for r in rows if r.kind == "stt")

        # The turn must actually have gone through the fast engine, or this
        # test isn't exercising the switch it claims to.
        assert stt.engine == "stub-meter-stt-fast"
        assert stt.engine != cfg.stt_engine

        # The bug: pairing the switched-to engine with the OLD engine's pinned
        # model. That pair was never used together and misses the registry
        # lookup. Recorded model_id must not be the stale pin.
        # Resolution finds no catalog default and no registry row for a stub
        # engine, so it stays blank -- the point of the assertion is that it
        # is not the stale pin.
        assert stt.model_id != "pinned-for-original-engine"
        assert stt.model_id == ""
    finally:
        stt_service.providers.pop("stub-meter-stt", None)
        stt_service.providers.pop("stub-meter-stt-fast", None)
        tts_service.providers.pop("stub-meter-tts", None)
