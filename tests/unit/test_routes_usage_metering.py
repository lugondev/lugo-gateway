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
    # No profile/registry override in scope -> engine/model best-effort "".
    assert row.engine == ""
    assert row.model_id == ""
