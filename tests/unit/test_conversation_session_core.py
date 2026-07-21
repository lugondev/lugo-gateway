import asyncio

import pytest
from app.schemas.stt import STTResult
from app.schemas.tts import TTSResult
from app.services.conversation.session import ConversationSession, SessionRuntimeConfig
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service

SR = 16000


class _StubSTT(STTProvider):
    name = "stub-core-stt"
    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chao", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-core-tts"
    async def synthesize(self, payload) -> TTSResult:
        return TTSResult(engine=self.name, sample_rate=24000,
                         audio_url="/artifacts/x.wav", duration_seconds=0.1, text=payload.text)


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    # conversation_llm_base_url now lives on system_config_store (Task 3), not
    # Settings; the module-level conftest._hermetic fixture already zeroes it.
    stt_service.providers["stub-core-stt"] = _StubSTT()
    tts_service.providers["stub-core-tts"] = _StubTTS()
    yield
    stt_service.providers.pop("stub-core-stt", None)
    tts_service.providers.pop("stub-core-tts", None)


def _cfg(**over):
    base = dict(
        session_id="s1", profile_name=None, stt_engine="stub-core-stt", language="vi",
        tts_engine="stub-core-tts", voice=None, ref_audio_path=None, ref_text=None,
        tts_instruct=None, tts_speed=None, tts_language=None, sample_rate=SR,
        output_sample_rate=24000, audio_codec="pcm16", want_audio=False, want_text=True,
        audio_out="url", denoise=False, resume_sid=None,
    )
    base.update(over)
    return SessionRuntimeConfig(**base)


class _RecordingSTT(STTProvider):
    name = "stub-record-stt"

    def __init__(self):
        self.seen_models: list[str | None] = []

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        self.seen_models.append(model)
        return STTResult(engine=self.name, text=f"heard:{model}", is_final=True)


async def test_two_sessions_transcribe_with_their_own_pinned_model():
    """Session A pins 'small', session B pins 'medium' on the same engine. Running
    a turn on each — even interleaved — must not let B's model affect A's turn."""
    provider = _RecordingSTT()
    stt_service.providers["stub-record-stt"] = provider
    try:
        events_a: list = []
        events_b: list = []

        async def emit_a(name, **p):
            events_a.append((name, p))

        async def emit_b(name, **p):
            events_b.append((name, p))

        async def noop_audio(pkt):
            pass

        sess_a = ConversationSession(
            _cfg(stt_engine="stub-record-stt", stt_model="small"), emit_a, noop_audio
        )
        sess_b = ConversationSession(
            _cfg(stt_engine="stub-record-stt", stt_model="medium"), emit_b, noop_audio
        )
        await sess_a.start()
        await sess_b.start()

        assert sess_a.stt_model_id == "small"
        assert sess_b.stt_model_id == "medium"

        # Drive an actual audio turn on each session (feed_text bypasses STT
        # entirely, so it wouldn't reach transcribe_bytes / prove model pinning).
        # Call _handle_turn directly -- the same entry point feed_audio's VAD
        # endpointer uses once it detects an endpoint -- so we deterministically
        # exercise the STT call without needing to fabricate VAD-triggering audio.
        pcm = b"\x00\x00" * 1600
        sess_a.current_turn = asyncio.create_task(sess_a._handle_turn(audio_pcm=pcm, speech_ms=300))
        await sess_a.wait_current_turn()
        sess_b.current_turn = asyncio.create_task(sess_b._handle_turn(audio_pcm=pcm, speech_ms=300))
        await sess_b.wait_current_turn()

        await sess_a.close()
        await sess_b.close()

        assert "small" in provider.seen_models
        assert "medium" in provider.seen_models
    finally:
        stt_service.providers.pop("stub-record-stt", None)


@pytest.mark.asyncio
async def test_text_turn_emits_transcript_and_reply():
    events = []
    async def emit(name, **p): events.append((name, p))
    async def emit_audio(pkt): events.append(("_audio", {"len": len(pkt)}))

    sess = ConversationSession(_cfg(), emit, emit_audio)
    await sess.start()
    await sess.feed_text("hello")
    await sess.wait_current_turn()
    await sess.close()

    names = [n for n, _ in events]
    assert "session_started" in names
    assert "user_transcript" in names
    assert "turn_done" in names


@pytest.mark.asyncio
async def test_session_start_pins_explicit_stt_model():
    """A cfg.stt_model override must be snapshotted onto the session as
    stt_model_id, not applied via a process-global mutation."""
    async def emit(name, **p): pass
    async def emit_audio(pkt): pass

    sess = ConversationSession(_cfg(stt_model="1.7b"), emit, emit_audio)
    await sess.start()
    await sess.close()

    assert sess.stt_model_id == "1.7b"


@pytest.mark.asyncio
async def test_session_started_reports_the_profiles_actual_llm_model(monkeypatch, tmp_path):
    """session_started's llm_model must reflect the model the responder was
    actually built with (self.responder.model), not the unrelated system-wide
    default (get_active_llm_model()) -- a profile with its own custom LLM
    model must show ITS model in the UI, not always "gpt-3.5-turbo"."""
    from app.services.profiles.models import LlmConfig, Profile
    from app.services.profiles.store import ProfileStore

    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    monkeypatch.setattr("app.services.conversation.session.profile_store", fresh_profiles)
    fresh_profiles.upsert(Profile(
        name="custom-model-profile",
        llm=LlmConfig(base_url="https://api.example.com/v1", api_key="k", model="custom-model-name"),
    ))

    events = []
    async def emit(name, **p): events.append((name, p))
    async def emit_audio(pkt): pass

    sess = ConversationSession(_cfg(profile_name="custom-model-profile"), emit, emit_audio)
    await sess.start()
    await sess.close()

    started = next(p for n, p in events if n == "session_started")
    assert started["llm_model"] == "custom-model-name"


@pytest.mark.asyncio
async def test_session_started_emits_friendly_registry_labels(monkeypatch, tmp_path):
    """session_started must carry the Model Registry's human labels
    (stt_label/tts_label/llm_label) so the admin chat header shows the same
    friendly name every profile/service select shows — never the raw engine
    name or a verbose provider "detail" string. When a registry row exists for
    the active (kind, engine, model), its label wins."""
    from app.services.model_registry.store import ModelRegistryStore
    from app.services.profiles.models import LlmConfig, Profile
    from app.services.profiles.store import ProfileStore

    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    monkeypatch.setattr("app.services.conversation.session.profile_store", fresh_profiles)
    fresh_profiles.upsert(Profile(
        name="labelled",
        llm=LlmConfig(base_url="https://api.example.com/v1", api_key="k",
                      model="gpt-4o-mini", engine="openai"),
    ))

    registry = ModelRegistryStore()
    await registry.create("stt", "stub-core-stt", "", "STT sentinel")  # config sentinel
    await registry.create("stt", "stub-core-stt", "small-v3", "Whisper Small v3", enabled=True)
    await registry.create("llm", "openai", "gpt-4o-mini", "OpenAI · GPT-4o mini")
    monkeypatch.setattr("app.services.conversation.session.model_registry_store", registry)

    events = []
    async def emit(name, **p): events.append((name, p))
    async def emit_audio(pkt): pass

    sess = ConversationSession(
        _cfg(profile_name="labelled", stt_engine="stub-core-stt", stt_model="small-v3"),
        emit, emit_audio,
    )
    await sess.start()
    await sess.close()

    started = next(p for n, p in events if n == "session_started")
    assert started["stt_label"] == "Whisper Small v3"
    assert started["llm_label"] == "OpenAI · GPT-4o mini"
    # Raw engine names must not leak into the friendly labels.
    assert "stub-core-stt" not in started["stt_label"]


@pytest.mark.asyncio
async def test_session_start_applies_registry_llm_override_for_profile(monkeypatch, tmp_path):
    """A profile that picks a Model Registry LLM (base_url/api_key cleared,
    engine+model set -- see profiles.js's registry-select save path) must have
    its responder built from the registry entry's base_url/api_key, exactly
    like the already-correct conversation.py/livehost.py call sites do. Before
    this fix, ConversationSession.start() never called
    resolve_llm_override_from_registry at all, so a registry-backed profile
    silently fell back to the system-wide default LLM base_url while still
    asking for the registry's model name -- producing a bogus offline error
    even though the registry entry itself is perfectly valid."""
    from app.services.model_registry.store import ModelRegistryStore
    from app.services.profiles.models import LlmConfig, Profile
    from app.services.profiles.store import ProfileStore

    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    monkeypatch.setattr("app.services.conversation.session.profile_store", fresh_profiles)
    fresh_profiles.upsert(Profile(
        name="registry-profile",
        llm=LlmConfig(base_url="", api_key="", model="openrouter/free", engine="openrouter"),
    ))

    registry = ModelRegistryStore()
    await registry.create(
        "llm", "openrouter", "openrouter/free", "OR free",
        api_key="sk-or-v1-real-key", base_url="https://openrouter.ai/api/v1",
    )

    async def emit(name, **p): pass
    async def emit_audio(pkt): pass

    sess = ConversationSession(_cfg(profile_name="registry-profile"), emit, emit_audio)
    await sess.start()
    await sess.close()

    assert sess.responder.base_url == "https://openrouter.ai/api/v1"
    assert sess.responder.api_key == "sk-or-v1-real-key"
    assert sess.responder.model == "openrouter/free"


@pytest.mark.asyncio
async def test_session_start_skips_apply_when_no_model_set():
    async def emit(name, **p): pass
    async def emit_audio(pkt): pass

    sess = ConversationSession(_cfg(), emit, emit_audio)  # stt_model defaults to ""
    await sess.start()  # must not raise
    await sess.close()

    assert sess.stt_model_id is None  # stub-core-stt has no model-variant registry


@pytest.mark.asyncio
async def test_session_start_does_not_mutate_the_process_global_active_model():
    """Regression guard: session.start() must resolve+snapshot stt_model_id for
    this session only, and must NOT flip the process-global active model the
    way the old apply_stt_model() call used to -- that's exactly the cross-session
    interference bug this task fixes (see test_stt_model_param_isolation.py for
    the provider-level half of this guarantee)."""
    from app.services.stt.providers import qwen3_asr_provider as q

    async def emit(name, **p): pass
    async def emit_audio(pkt): pass

    original = q.get_active_qwen3_asr_model()
    sess = ConversationSession(_cfg(stt_engine="qwen3_asr", stt_model="1.7b"), emit, emit_audio)
    try:
        await sess.start()
        assert sess.stt_model_id == "1.7b"
        assert q.get_active_qwen3_asr_model() == original
    finally:
        await sess.close()
        q.set_active_qwen3_asr_model(None)
