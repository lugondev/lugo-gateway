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

from app.core.audio import pcm16_to_wav_bytes
from app.schemas.stt import STTResult
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


def _silence_wav(ms: int = 100, sr: int = 24000) -> bytes:
    n = int(sr * ms / 1000)
    return pcm16_to_wav_bytes(b"\x00\x00" * n, sample_rate=sr)


class _StubSTT(STTProvider):
    name = "stub-meter-stt"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chao", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-meter-tts"

    def __init__(self) -> None:
        # One entry per real synthesis: an over-quota skip has to be provable by
        # "the provider was never called", not only by "no row was written" -- a
        # call that happened and went unrecorded is the bug being closed.
        self.calls: list[str] = []

    async def render_audio(self, payload) -> tuple[bytes, str]:
        self.calls.append(payload.text)
        return _silence_wav(), "audio/wav"


def _cfg(**over):
    base = dict(
        session_id="s1", profile_name="metered-profile", stt_engine="stub-meter-stt", language="vi",
        tts_engine="stub-meter-tts", voice=None, ref_audio_path=None, ref_text=None,
        tts_instruct=None, tts_speed=None, tts_language=None, sample_rate=SR,
        output_sample_rate=24000, audio_codec="pcm16", want_audio=True, want_text=True,
        audio_out="wav", denoise=False, resume_sid=None, identity_user_id="user-42",
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
    # usable_profile_or_none now requires shared or owner match -- bind the
    # profile to the same identity_user_id _cfg() authenticates as ("user-42"),
    # matching Root Cause A of task-3b-brief.md.
    fresh_profiles.upsert(Profile(
        name="metered-profile",
        owner_id="user-42",
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


async def test_llm_usage_names_the_responders_model_when_the_profile_pins_none(
    monkeypatch, tmp_path
):
    """A profile with no llm.model still runs a real model (build_responder_ex
    falls back to the registry default). The usage row must name THAT model,
    read off the responder, not blank."""
    stt_service.providers["stub-attr-stt"] = _StubSTT()
    tts_service.providers["stub-attr-tts"] = _StubTTS()

    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    monkeypatch.setattr("app.services.conversation.session.profile_store", fresh_profiles)
    # No llm.model and no llm.engine -- the case that produced ('', '') rows.
    fresh_profiles.upsert(Profile(name="attr-profile", llm=LlmConfig()))

    try:
        events: list = []

        async def emit(name, **p):
            events.append((name, p))

        async def emit_audio(pkt):
            pass

        # profile_name/stt_engine/tts_engine must match the profile and stub
        # providers registered above -- this file's _cfg() defaults to the
        # OTHER neighboring tests' names ("metered-profile", "stub-meter-*").
        cfg = _cfg(profile_name="attr-profile", stt_engine="stub-attr-stt", tts_engine="stub-attr-tts")
        sess = ConversationSession(cfg, emit, emit_audio)
        await sess.start()
        # Stand in for whatever responder build_responder_ex returned, with the
        # model attribute a real OpenAICompatResponder carries.
        sess.responder.model = "resolved-by-responder"
        sess.responder.last_usage = {"prompt_tokens": 11, "completion_tokens": 3}
        await sess._record_llm_usage()
        await sess.close()

        rows = await _rows()
        llm = next(r for r in rows if r.kind == "llm")
        assert llm.model_id == "resolved-by-responder"
        assert llm.prompt_tokens == 11 and llm.completion_tokens == 3
    finally:
        stt_service.providers.pop("stub-attr-stt", None)
        tts_service.providers.pop("stub-attr-tts", None)


async def test_profile_pinned_model_used_when_responder_has_no_model_attr(
    monkeypatch, tmp_path
):
    """With no base_url and no seeded Model Registry default, build_responder_ex
    returns the real EchoResponder, which carries no `.model` attribute at all
    (unlike OpenAICompatResponder). getattr(responder, "model", "") is then ""
    for both the pre-fix and post-fix resolution, so this only locks in the
    fallback-to-profile-pin path, not the responder-wins path (see the
    differs-from-pin test below for that)."""
    stt_service.providers["stub-attr2-stt"] = _StubSTT()
    tts_service.providers["stub-attr2-tts"] = _StubTTS()
    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    monkeypatch.setattr("app.services.conversation.session.profile_store", fresh_profiles)
    # usable_profile_or_none now requires shared or owner match -- bind the
    # profile to the same identity_user_id _cfg() authenticates as ("user-42"),
    # matching Root Cause A of task-3b-brief.md.
    fresh_profiles.upsert(Profile(
        name="attr2-profile",
        owner_id="user-42",
        llm=LlmConfig(model="pinned-model", engine="pinned-engine"),
    ))
    try:
        async def emit(name, **p):
            pass

        async def emit_audio(pkt):
            pass

        cfg = _cfg(profile_name="attr2-profile", stt_engine="stub-attr2-stt", tts_engine="stub-attr2-tts")
        sess = ConversationSession(cfg, emit, emit_audio)
        await sess.start()
        assert not hasattr(sess.responder, "model")  # sanity: EchoResponder, not stamped
        sess.responder.last_usage = {"prompt_tokens": 4, "completion_tokens": 1}
        await sess._record_llm_usage()
        await sess.close()
        rows = await _rows()
        llm = next(r for r in rows if r.kind == "llm")
        assert (llm.engine, llm.model_id) == ("pinned-engine", "pinned-model")
    finally:
        stt_service.providers.pop("stub-attr2-stt", None)
        tts_service.providers.pop("stub-attr2-tts", None)


async def test_llm_usage_names_the_responders_model_even_when_it_differs_from_the_pin(
    monkeypatch, tmp_path
):
    """The discriminating case: a profile pins one model, but the responder
    that actually ran the turn carries a DIFFERENT model string (e.g. the pin
    was resolved through a registry override, or is simply stale). The model
    actually sent to the provider -- read off the responder -- must win over
    the profile pin.

    `engine` must NOT come from the profile here. The profile's engine labels
    the row the profile pinned; once the model differs, that engine belongs to
    a different registry row, and pairing the two would price the call at the
    wrong provider's rate and charge the wrong provider's quota. Blank engine
    hands the pair to resolve_usage_model(), whose reverse model->engine lookup
    names the engine that actually owns the responder's model -- and, when no
    registry row claims that model (as here), leaves engine blank rather than
    asserting a coherent-looking but false pairing."""
    stt_service.providers["stub-attr3-stt"] = _StubSTT()
    tts_service.providers["stub-attr3-tts"] = _StubTTS()
    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    monkeypatch.setattr("app.services.conversation.session.profile_store", fresh_profiles)
    fresh_profiles.upsert(Profile(
        name="attr3-profile",
        llm=LlmConfig(model="pinned-model", engine="pinned-engine"),
    ))
    try:
        async def emit(name, **p):
            pass

        async def emit_audio(pkt):
            pass

        cfg = _cfg(profile_name="attr3-profile", stt_engine="stub-attr3-stt", tts_engine="stub-attr3-tts")
        sess = ConversationSession(cfg, emit, emit_audio)
        await sess.start()
        sess.responder.model = "actually-called-model"
        sess.responder.last_usage = {"prompt_tokens": 4, "completion_tokens": 1}
        await sess._record_llm_usage()
        await sess.close()
        rows = await _rows()
        llm = next(r for r in rows if r.kind == "llm")
        assert llm.model_id == "actually-called-model"
        assert llm.engine == ""
    finally:
        stt_service.providers.pop("stub-attr3-stt", None)
        tts_service.providers.pop("stub-attr3-tts", None)


async def test_unpinned_model_is_attributed_to_the_registrys_engine_not_the_profiles(
    monkeypatch, tmp_path
):
    """The mis-billing scenario: the profile pins an ENGINE but no model, so
    build_responder_ex resolved the model from the Model Registry default --
    and that model lives on a DIFFERENT engine than the profile names.

    Pairing the profile's engine with the registry's model would make
    find(kind, engine, model_id) match the profile engine's row, pricing the
    call at that provider's rate and debiting that provider's quota for a
    request it never served. The row must name the engine that actually owns
    the model. Without the fix this records "profile-engine"."""
    from app.services.db.engine import init_db
    from app.services.model_registry.store import model_registry_store

    stt_service.providers["stub-attr4-stt"] = _StubSTT()
    tts_service.providers["stub-attr4-tts"] = _StubTTS()
    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    monkeypatch.setattr("app.services.conversation.session.profile_store", fresh_profiles)
    # Engine pinned, model NOT pinned -- the exact shape that mis-bills.
    fresh_profiles.upsert(Profile(
        name="attr4-profile", llm=LlmConfig(engine="profile-engine"),
    ))
    await init_db()
    # The registry says this model belongs to a different engine. Not is_default,
    # so _active_llm_entry() still finds nothing and build_responder_ex keeps
    # returning the in-process EchoResponder (no network I/O in this test).
    await model_registry_store.create("llm", "registry-engine", "registry-model", "Registry")
    try:
        async def emit(name, **p):
            pass

        async def emit_audio(pkt):
            pass

        cfg = _cfg(profile_name="attr4-profile", stt_engine="stub-attr4-stt", tts_engine="stub-attr4-tts")
        sess = ConversationSession(cfg, emit, emit_audio)
        await sess.start()
        sess.responder.model = "registry-model"
        sess.responder.last_usage = {"prompt_tokens": 7, "completion_tokens": 2}
        await sess._record_llm_usage()
        await sess.close()
        rows = await _rows()
        llm = next(r for r in rows if r.kind == "llm")
        assert llm.model_id == "registry-model"
        assert llm.engine == "registry-engine"
        assert llm.engine != "profile-engine"
    finally:
        stt_service.providers.pop("stub-attr4-stt", None)
        tts_service.providers.pop("stub-attr4-tts", None)


# --- speak(): the idle farewell -------------------------------------------------
#
# speak() is the one-off spoken utterance the SERVER initiates at teardown (an
# idle goodbye), outside any turn -- so it never passed through the per-turn
# quota gate and never recorded a usage row, while still calling the TTS
# provider for real. Same shape as post-session memory work: meter it, and treat
# an over-quota state as "skip quietly" (nobody is waiting on a farewell, so a
# refusal is silence, never an error).


async def test_the_farewell_utterance_records_a_usage_row(monkeypatch, tmp_path):
    from app.services.db.engine import init_db

    stub = _StubTTS()
    stt_service.providers["stub-meter-stt"] = _StubSTT()
    tts_service.providers["stub-meter-tts"] = stub

    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    monkeypatch.setattr("app.services.conversation.session.profile_store", fresh_profiles)
    fresh_profiles.upsert(Profile(
        name="metered-profile",
        llm=LlmConfig(base_url="", api_key="", model="echo-model", engine="echo-engine"),
    ))
    await init_db()
    try:
        async def emit(name, **p):
            pass

        async def emit_audio(pkt):
            pass

        sess = ConversationSession(_cfg(), emit, emit_audio)
        await sess.start()
        farewell = "tam biet nhe"
        await sess.speak(farewell)
        await sess.close()

        assert stub.calls == [farewell], "the farewell must actually be synthesized"
        tts_rows = [r for r in await _rows() if r.kind == "tts"]
        assert len(tts_rows) == 1, f"expected one row for the farewell, got {len(tts_rows)}"
        row = tts_rows[0]
        assert row.engine == "stub-meter-tts"
        assert row.unit == "chars"
        assert row.native_amount == len(farewell)
        assert row.user_id == "user-42"
        assert row.profile_id == "metered-profile"
    finally:
        stt_service.providers.pop("stub-meter-stt", None)
        tts_service.providers.pop("stub-meter-tts", None)


async def test_an_over_quota_session_skips_the_farewell_without_erroring(monkeypatch, tmp_path):
    """Over quota, the farewell must not reach the provider at all -- and must
    not become an error either: no exception, no `error` event, and the session
    still closes cleanly. A limit that only stops the work the user is waiting
    for is not a limit."""
    from app.services.db.engine import init_db
    from app.services.model_registry.store import model_registry_store
    from app.services.quota.store import quota_store
    from app.services.usage.recorder import record_usage

    stub = _StubTTS()
    stt_service.providers["stub-meter-stt"] = _StubSTT()
    tts_service.providers["stub-meter-tts"] = stub

    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    monkeypatch.setattr("app.services.conversation.session.profile_store", fresh_profiles)
    fresh_profiles.upsert(Profile(
        name="metered-profile",
        llm=LlmConfig(base_url="", api_key="", model="echo-model", engine="echo-engine"),
    ))
    await init_db()
    quota_store.invalidate()
    # A priced TTS row plus enough recorded spend to blow a $1 global limit:
    # $1000 per 1k chars x 1000 chars = $1000 already spent.
    await model_registry_store.create(
        "tts", "stub-meter-tts", "stub-tts-model", "Stub",
        config={"provider_id": "prov-t", "price": {"unit": "1k_chars", "rate": 1000.0}},
    )
    await record_usage(user_id="user-42", profile_id="metered-profile", kind="tts",
                       engine="stub-meter-tts", model_id="stub-tts-model",
                       unit="chars", native_amount=1000)
    await quota_store.create(scope="global", scope_id="", limit_usd=1.0, period="monthly")

    try:
        events: list = []

        async def emit(name, **p):
            events.append((name, p))

        async def emit_audio(pkt):
            pass

        sess = ConversationSession(_cfg(), emit, emit_audio)
        await sess.start()
        farewell = "tam biet nhe"
        await sess.speak(farewell)  # must not raise
        await sess.close()

        assert stub.calls == [], f"over quota, the provider must not be called: {stub.calls}"
        assert [n for n, _ in events if n == "error"] == [], "a skipped farewell is not an error"
        served = [r for r in await _rows()
                  if r.kind == "tts" and r.status == "ok" and r.native_amount == len(farewell)]
        assert served == [], "nothing was synthesized, so nothing may be billed"
        # Silence, not a half-spoken turn: no audio and no farewell text went out.
        assert [n for n, _ in events if n in {"audio_start", "audio_chunk", "response_text"}] == []
    finally:
        stt_service.providers.pop("stub-meter-stt", None)
        tts_service.providers.pop("stub-meter-tts", None)
