"""One test per externally reachable paid entry point, each asserting a real row
lands in usage_events.

test_paid_call_site_inventory.py proves every call site was classified; this
proves the classification is true where a client can reach it. Both are needed:
the first catches an omission, the second catches a lie.

Deliberately NOT covered here (each has its own dedicated suite, named so a
reader can check): the conversation core (tests/unit/conversation/test_session_usage_metering.py
-- the livehost plugin's own traffic reaches this exact code path too, over
/v1/conversation/stream, so this file's coverage covers it as well) and the
memory subsystem (tests/unit/memory/test_memory_usage_metering.py). Those run
over a WebSocket or a session teardown that this file's harness cannot drive.

Harness notes (adaptations to the brief, made for reasons already root-caused
on this branch):

- STTRequest.engine is regex-restricted to known engine ids (see
  app/schemas/stt.py), so the REST /transcribe path below registers the STT
  stub under the real "vosk" key (swapped back after the test), the same
  pattern tests/unit/usage/test_routes_usage_metering.py already uses. The
  WebSocket /stream path builds no STTRequest (engine is a raw query param),
  so it keeps a made-up engine id.
- STTResult requires an `engine` field; the brief's stub text omits it, added
  here.
- The STT WS test drains until "done", not "final": the usage row is written
  between the "final" and "done" events on the end path, so stopping at
  "final" would race the server's pending write against the test client's
  teardown (see tests/unit/stt/test_stt_stream_metering.py).
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.audio import pcm16_to_wav_bytes
from app.main import app
from app.schemas.stt import STTResult
from app.services.db.engine import db_session, init_db
from app.services.db.models import UsageEvent
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service


class _StubSTT(STTProvider):
    # Registered under the real "vosk" key for the REST /transcribe test --
    # see the module docstring's harness notes.
    name = "vosk"

    def available(self) -> bool:
        return True

    async def transcribe_bytes(self, audio: bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chao", is_final=True)


class _StubStreamSTT(STTProvider):
    """Used only by the WS test, which builds no STTRequest and so isn't
    subject to the engine-id regex -- a made-up name is fine here."""

    name = "stub-e2e-stt"

    def available(self) -> bool:
        return True

    async def transcribe_bytes(self, audio: bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chao", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-e2e-tts"

    def available(self) -> bool:
        return True

    async def render_audio(self, request) -> tuple[bytes, str]:
        # /v1/tts/synthesize now calls this bytes-returning seam directly
        # (see app.services.tts.base.TTSProvider.render_audio). Must be a
        # real WAV -- the route computes duration via wav_duration_seconds.
        return pcm16_to_wav_bytes(b"\x00\x00" * 10, sample_rate=16000), "audio/wav"


@pytest.fixture
def _stubs():
    original_vosk = stt_service.providers.get(_StubSTT.name)
    stt_service.providers[_StubSTT.name] = _StubSTT()
    stt_service.providers[_StubStreamSTT.name] = _StubStreamSTT()
    tts_service.providers[_StubTTS.name] = _StubTTS()
    yield
    if original_vosk is not None:
        stt_service.providers[_StubSTT.name] = original_vosk
    else:
        stt_service.providers.pop(_StubSTT.name, None)
    stt_service.providers.pop(_StubStreamSTT.name, None)
    tts_service.providers.pop(_StubTTS.name, None)


async def _rows():
    async with db_session() as s:
        return list((await s.execute(select(UsageEvent))).scalars().all())


async def test_transcribe_meters(_stubs):
    await init_db()
    client = TestClient(app)
    wav = pcm16_to_wav_bytes(b"\x00\x00" * 1600, sample_rate=16000)
    resp = client.post("/v1/stt/transcribe", files={"audio": ("a.wav", wav, "audio/wav")},
                       data={"engine": _StubSTT.name})
    assert resp.status_code == 200, resp.text
    stt = [r for r in await _rows() if r.kind == "stt"]
    assert len(stt) == 1 and stt[0].native_amount > 0


async def test_synthesize_meters(_stubs):
    await init_db()
    client = TestClient(app)
    resp = client.post("/v1/tts/synthesize", json={"text": "xin chao", "engine": "stub-e2e-tts"})
    assert resp.status_code == 200, resp.text
    tts = [r for r in await _rows() if r.kind == "tts"]
    assert len(tts) == 1 and tts[0].native_amount == len("xin chao")


async def test_stt_stream_socket_meters(_stubs):
    import json

    await init_db()
    client = TestClient(app)
    with client.websocket_connect(
        "/v1/stt/stream?engine=stub-e2e-stt&sample_rate=16000&denoise=false&vad=false"
    ) as ws:
        assert ws.receive_json()["event_type"] == "session_started"
        ws.send_bytes(b"\x00\x00" * 1600)
        ws.send_text(json.dumps({"type": "end"}))
        # Drain until "done", not "final": see the module docstring's harness
        # notes -- the usage row is written before "done" is emitted, so
        # "done" proves the write completed without racing teardown.
        for _ in range(5):
            if ws.receive_json()["event_type"] == "done":
                break
    stt = [r for r in await _rows() if r.kind == "stt"]
    assert stt, "the streaming socket recorded nothing"


async def test_chat_meters():
    await init_db()
    client = TestClient(app)
    resp = client.post("/v1/conversation/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200, resp.text
    llm = [r for r in await _rows() if r.kind == "llm"]
    assert len(llm) == 1
