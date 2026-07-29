"""Task 6: /chat, /transcribe, /synthesize must each record a best-effort
UsageEvent after the work completes.

Approach: drive /transcribe and /synthesize (and /chat, which needs no
provider setup since the default EchoResponder is used) via the sync
TestClient with stub STT/TTS providers registered directly on the service
singletons -- same pattern as tests/unit/test_model_registry_routes.py and
tests/unit/test_conversation_profile.py. `_tmp_db` (tests/conftest.py,
autouse) points the DB at a fresh per-test sqlite file, so we assert real
rows in `usage_events` rather than a record_usage spy -- no route here needs
heavy provider setup, so the brief's spy fallback isn't needed.
"""

from sqlalchemy import select
from fastapi.testclient import TestClient

from app.core.audio import pcm16_to_wav_bytes
from app.main import app
from app.schemas.stt import STTResult
from app.schemas.tts import TTSResult
from app.services.db.engine import db_session
from app.services.db.models import UsageEvent
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service

SR = 16000


class _StubSTT(STTProvider):
    # STTRequest.engine is regex-restricted to known engine ids (see
    # app/schemas/stt.py), so this stub is registered under the real "vosk"
    # key (swapped back after each test) rather than a made-up name.
    name = "vosk"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chao", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-meter-route-tts"

    async def synthesize(self, payload) -> TTSResult:
        return TTSResult(
            engine=self.name, sample_rate=24000, audio_url="/artifacts/x.wav",
            duration_seconds=0.1, text=payload.text,
        )

    async def render_audio(self, payload) -> tuple[bytes, str]:
        # /v1/tts/synthesize now calls this bytes-returning seam directly
        # (see app.services.tts.base.TTSProvider.render_audio). Must be a
        # real WAV -- the route computes duration via wav_duration_seconds.
        return pcm16_to_wav_bytes(b"\x00\x00" * 10, sample_rate=24000), "audio/wav"


async def _rows() -> list[UsageEvent]:
    async with db_session() as s:
        return list((await s.execute(select(UsageEvent))).scalars().all())


def _wav_bytes(ms: int = 300) -> bytes:
    n = int(SR * ms / 1000)
    pcm = b"\x00\x00" * n
    return pcm16_to_wav_bytes(pcm, sample_rate=SR)


async def test_transcribe_records_stt_usage_event():
    original = stt_service.providers.get(_StubSTT.name)
    stt_service.providers[_StubSTT.name] = _StubSTT()
    client = TestClient(app)
    try:
        resp = client.post(
            "/v1/stt/transcribe",
            files={"audio": ("clip.wav", _wav_bytes(), "audio/wav")},
            data={"engine": _StubSTT.name, "denoise": "false", "vad": "false"},
        )
        assert resp.status_code == 200

        rows = await _rows()
        stt_rows = [r for r in rows if r.kind == "stt"]
        assert len(stt_rows) == 1
        row = stt_rows[0]
        assert row.engine == _StubSTT.name
        assert row.unit == "seconds"
        assert row.native_amount > 0
        assert row.user_id == ""  # no auth in test env -> current_user_id is None -> ""
    finally:
        if original is not None:
            stt_service.providers[_StubSTT.name] = original
        else:
            stt_service.providers.pop(_StubSTT.name, None)


async def test_synthesize_records_tts_usage_event():
    tts_service.providers[_StubTTS.name] = _StubTTS()
    client = TestClient(app)
    try:
        text = "xin chào thế giới"
        resp = client.post("/v1/tts/synthesize", json={"text": text, "engine": _StubTTS.name})
        assert resp.status_code == 200

        rows = await _rows()
        tts_rows = [r for r in rows if r.kind == "tts"]
        assert len(tts_rows) == 1
        row = tts_rows[0]
        assert row.engine == _StubTTS.name
        assert row.unit == "chars"
        assert row.native_amount == len(text)
    finally:
        tts_service.providers.pop(_StubTTS.name, None)


async def test_chat_tool_path_records_llm_usage_event(monkeypatch):
    """Review finding I1: the tool-enabled `/chat` branch (`reply_stream`) must
    record usage same as the non-tool `reply()` branch.

    Approach: force `_build_tool_registry` to return a non-empty registry (flip
    the shared `settings.conversation_tools_enabled` singleton, which adds the
    built-in `LocalToolSource` with no other setup needed) so `/chat` takes the
    `if tool_registry:` branch, and stub `build_responder_ex` (patched on the
    route module, where it's called) with a fake responder whose
    `reply_stream` yields a chunk and populates `last_usage` -- mirroring what
    `OpenAICompatResponder._stream_history` does for real. This avoids standing
    up a fake LLM HTTP backend for the full 2-phase tool-calling loop while
    still exercising the exact route code path added for I1.
    """
    from app.api.routes import conversation as conversation_routes
    from app.core.settings import settings as shared_settings
    from app.services.conversation.responder import Responder

    class _StubToolResponder(Responder):
        name = "llm"

        def __init__(self) -> None:
            self.last_usage: dict | None = None

        async def reply(self, history: list[dict]) -> str:  # pragma: no cover - unused here
            return "unused"

        async def reply_stream(self, history, registry=None, ctx=None, max_iters=3):
            self.last_usage = {"prompt_tokens": 11, "completion_tokens": 7}
            yield "tool reply."

    async def _fake_build_responder_ex(**kwargs):
        return _StubToolResponder()

    monkeypatch.setattr(conversation_routes, "build_responder_ex", _fake_build_responder_ex)
    monkeypatch.setattr(shared_settings, "conversation_tools_enabled", True)

    client = TestClient(app)
    resp = client.post("/v1/conversation/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    assert resp.json()["data"]["reply"] == "tool reply."  # sanity: tool branch (reply_stream) was taken

    rows = await _rows()
    llm_rows = [r for r in rows if r.kind == "llm"]
    assert len(llm_rows) == 1
    row = llm_rows[0]
    assert row.unit == "tokens"
    assert row.prompt_tokens == 11
    assert row.completion_tokens == 7
    assert row.native_amount == 18


async def test_chat_non_tool_path_records_llm_usage_event():
    client = TestClient(app)
    resp = client.post("/v1/conversation/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    assert resp.json()["data"]["responder"] == "echo"  # sanity: non-tool `reply()` path taken

    rows = await _rows()
    llm_rows = [r for r in rows if r.kind == "llm"]
    assert len(llm_rows) == 1
    row = llm_rows[0]
    assert row.unit == "tokens"
    # No profile, no registry default, and an EchoResponder -> genuinely nothing to resolve.
    assert row.engine == ""
    assert row.model_id == ""


async def test_chat_with_no_profile_yields_a_priceable_row():
    """Guards the shape behind the production rows that read
    `llm / - / (not recorded)`: POST /chat without ?profile= answers with a real
    model but the route knows no engine or model to name.

    The resolution here comes from record_usage's active-default rule, not from
    the route -- with no profile there is no engine to mis-pair, so this test
    would also pass against the route's older code. It is kept as an end-to-end
    guard that the blank-blank shape still lands on a costed row; the route's
    own pairing logic is covered by the next test.
    """
    from app.services.db.engine import init_db
    from app.services.model_registry.store import model_registry_store

    await init_db()
    await model_registry_store.create(
        "llm", "OA", "gpt-4o-mini", "OpenAI mini",
        config={"provider_id": "prov-oa", "price": {"unit": "1M_tokens", "in": 1.0, "out": 2.0}},
        is_default=True,
    )

    client = TestClient(app)
    resp = client.post("/v1/conversation/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200

    llm_rows = [r for r in await _rows() if r.kind == "llm"]
    assert len(llm_rows) == 1
    row = llm_rows[0]
    # Resolved through the registry default rather than left blank, which is
    # also what makes the row priceable at all.
    assert (row.engine, row.model_id) == ("OA", "gpt-4o-mini")
    assert row.provider_id == "prov-oa"


async def test_rest_metering_records_the_profile_when_given_one():
    """Without this the profile dimension of the dashboards only ever has data
    from the conversation core, so a REST-driven integration is invisible in it.

    Registered under the real "vosk" engine id (see _StubSTT), not a made-up
    name, because STTRequest.engine is regex-restricted -- the original is
    saved/restored the same way test_transcribe_records_stt_usage_event does."""
    original = stt_service.providers.get(_StubSTT.name)
    stt_service.providers[_StubSTT.name] = _StubSTT()
    try:
        client = TestClient(app)
        wav = pcm16_to_wav_bytes(b"\x00\x00" * 1600, sample_rate=16000)
        resp = client.post(
            "/v1/stt/transcribe?profile=my-profile",
            files={"audio": ("a.wav", wav, "audio/wav")},
            data={"engine": _StubSTT.name},
        )
        assert resp.status_code == 200, resp.text
        stt = [r for r in await _rows() if r.kind == "stt"]
        assert len(stt) == 1
        assert stt[0].profile_id == "my-profile"
    finally:
        if original is not None:
            stt_service.providers[_StubSTT.name] = original
        else:
            stt_service.providers.pop(_StubSTT.name, None)


async def test_synthesize_records_the_profile_when_given_one():
    tts_service.providers["stub-prof-tts"] = _StubTTS()
    try:
        client = TestClient(app)
        resp = client.post(
            "/v1/tts/synthesize?profile=my-profile",
            json={"text": "xin chao", "engine": "stub-prof-tts"},
        )
        assert resp.status_code == 200, resp.text
        tts = [r for r in await _rows() if r.kind == "tts"]
        assert len(tts) == 1
        assert tts[0].profile_id == "my-profile"
    finally:
        tts_service.providers.pop("stub-prof-tts", None)


async def test_chat_never_pairs_a_profile_engine_with_an_unpinned_model():
    """A profile pinning an engine but no model must not have that engine
    stamped onto the registry-default model: the row would be priced at this
    provider's rate for a call another provider served."""
    from app.services.db.engine import init_db
    from app.services.model_registry.store import model_registry_store
    from app.services.profiles.models import LlmConfig, Profile
    from app.services.profiles.store import profile_store

    await init_db()
    await model_registry_store.create(
        "llm", "OA", "default-model", "OpenAI",
        config={"provider_id": "prov-oa"}, is_default=True,
    )
    await model_registry_store.create(
        "llm", "openrouter", "or/pricey", "OpenRouter",
        config={"provider_id": "prov-or", "price": {"unit": "1M_tokens", "in": 99.0, "out": 99.0}},
    )
    profile_store.upsert(Profile(name="engine-only", llm=LlmConfig(engine="openrouter")))
    try:
        client = TestClient(app)
        resp = client.post(
            "/v1/conversation/chat?profile=engine-only",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200
        llm_rows = [r for r in await _rows() if r.kind == "llm"]
        assert len(llm_rows) == 1
        # NOT ("openrouter", "default-model") -- that pair never ran.
        assert llm_rows[0].engine != "openrouter"
        assert llm_rows[0].provider_id != "prov-or"
    finally:
        profile_store.delete("engine-only")
