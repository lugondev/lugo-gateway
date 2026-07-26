"""WS /v1/stt/stream was the last STT path that spent money with no usage row
and no quota check. A long-lived socket is the worst place for that gap: it can
transcribe indefinitely."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.schemas.stt import STTResult
from app.services.db.engine import db_session, init_db
from app.services.db.models import UsageEvent
from app.services.model_registry.store import model_registry_store
from app.services.quota.store import quota_store
from app.services.stt.base import STTProvider, STTStream
from app.services.stt.service import stt_service
from app.services.usage.recorder import record_usage


class _StubStream(STTStream):
    async def accept(self, frame: bytes):
        return []

    async def finalize(self) -> STTResult:
        return STTResult(engine="stub-stream-stt", text="xin chao", is_final=True)


class _StubSTT(STTProvider):
    name = "stub-stream-stt"

    def available(self) -> bool:
        return True

    async def transcribe_bytes(self, audio: bytes, language=None, model=None) -> STTResult:
        return STTResult(engine="stub-stream-stt", text="xin chao", is_final=True)

    def open_stream(self, sample_rate, language=None, model=None):
        return _StubStream()


@pytest.fixture
def _stub_engine():
    stt_service.providers["stub-stream-stt"] = _StubSTT()
    yield
    stt_service.providers.pop("stub-stream-stt", None)


async def _rows(kind="stt"):
    async with db_session() as s:
        rows = (await s.execute(select(UsageEvent))).scalars().all()
    return [r for r in rows if r.kind == kind]


# 16 kHz mono PCM16: 3200 bytes = 1600 samples = 0.1 s
_FRAME = b"\x00\x00" * 1600


async def test_stream_records_the_audio_seconds_it_received(_stub_engine):
    await init_db()
    quota_store.invalidate()
    client = TestClient(app)
    with client.websocket_connect(
        "/v1/stt/stream?engine=stub-stream-stt&sample_rate=16000&denoise=false&vad=false"
    ) as ws:
        assert ws.receive_json()["event_type"] == "session_started"
        for _ in range(5):  # 5 x 0.1 s = 0.5 s
            ws.send_bytes(_FRAME)
        ws.send_text(json.dumps({"type": "end"}))
        # Drain until "done": that event is emitted strictly after the server
        # has recorded usage for this finalize (record happens between the
        # "final" and "done" events on the end path), so stopping at "final"
        # would race the server's pending write against the client closing
        # the socket.
        for _ in range(5):
            event = ws.receive_json()
            if event["event_type"] == "done":
                break

    rows = await _rows()
    assert len(rows) == 1, f"expected one row per finalize, got {len(rows)}"
    assert rows[0].engine == "stub-stream-stt"
    assert rows[0].unit == "seconds"
    assert abs(rows[0].native_amount - 0.5) < 1e-6


async def test_audio_is_counted_before_preprocessing_can_shrink_it(_stub_engine, monkeypatch):
    """In this codebase, preprocess_pcm16 always returns a frame the same byte
    length as its input (vad_gate zeroes samples in place rather than dropping
    them; denoising doesn't change frame length either) -- so a test that only
    sends silence through real VAD can't actually distinguish "counted before
    preprocessing" from "counted after": both give the same byte length. Force
    a real difference by patching preprocess_pcm16 to return an empty frame,
    and require the metered row to still reflect what the client sent, not the
    (bogus, shrunk) "processed" result. This fails if the accumulator is ever
    moved below the preprocess_pcm16 call."""
    import app.api.routes.stt as stt_route

    monkeypatch.setattr(stt_route, "preprocess_pcm16", lambda *a, **k: b"")

    await init_db()
    quota_store.invalidate()
    client = TestClient(app)
    with client.websocket_connect(
        "/v1/stt/stream?engine=stub-stream-stt&sample_rate=16000&denoise=false&vad=true"
    ) as ws:
        assert ws.receive_json()["event_type"] == "session_started"
        for _ in range(3):
            ws.send_bytes(_FRAME)
        ws.send_text(json.dumps({"type": "end"}))
        # See test_stream_records_the_audio_seconds_it_received: "done" (not
        # "final") is the event that follows the server's usage record on the
        # end path.
        for _ in range(5):
            if ws.receive_json()["event_type"] == "done":
                break

    rows = await _rows()
    assert len(rows) == 1
    assert abs(rows[0].native_amount - 0.3) < 1e-6, "must count received audio, not preprocessed audio"


async def test_two_flushes_produce_two_rows_without_double_counting(_stub_engine):
    await init_db()
    quota_store.invalidate()
    client = TestClient(app)
    with client.websocket_connect(
        "/v1/stt/stream?engine=stub-stream-stt&sample_rate=16000&denoise=false&vad=false"
    ) as ws:
        assert ws.receive_json()["event_type"] == "session_started"
        for _ in range(2):
            ws.send_bytes(_FRAME)
        ws.send_text(json.dumps({"type": "flush"}))
        for _ in range(5):
            if ws.receive_json()["event_type"] == "final":
                break
        for _ in range(3):
            ws.send_bytes(_FRAME)
        ws.send_text(json.dumps({"type": "end"}))
        # "done", not "final": see test_stream_records_the_audio_seconds_it_received.
        for _ in range(5):
            if ws.receive_json()["event_type"] == "done":
                break

    rows = sorted(await _rows(), key=lambda r: r.native_amount)
    assert len(rows) == 2, f"expected one row per flush, got {len(rows)}"
    assert abs(rows[0].native_amount - 0.2) < 1e-6
    assert abs(rows[1].native_amount - 0.3) < 1e-6
    total = sum(r.native_amount for r in rows)
    assert abs(total - 0.5) < 1e-6, "the same audio must not be counted twice"


async def test_an_over_quota_socket_is_refused_before_any_audio(_stub_engine):
    await init_db()
    quota_store.invalidate()
    await model_registry_store.create(
        "stt", "stub-stream-stt", "stub-model", "Stub",
        config={"provider_id": "prov-s", "price": {"unit": "minute", "rate": 60.0}},
    )
    await record_usage(user_id="", profile_id="", kind="stt", engine="stub-stream-stt",
                       model_id="stub-model", unit="seconds", native_amount=120)  # $120
    await quota_store.create(scope="global", scope_id="", limit_usd=1.0, period="monthly")

    client = TestClient(app)
    with client.websocket_connect(
        "/v1/stt/stream?engine=stub-stream-stt&sample_rate=16000&denoise=false&vad=false"
    ) as ws:
        event = ws.receive_json()
        assert event["event_type"] == "error", f"expected a refusal, got {event}"
        assert "quota exceeded" in event["payload"]["message"]

    # The refusal itself must be audited, and must not have transcribed anything.
    blocked = [r for r in await _rows() if r.status == "blocked"]
    assert len(blocked) == 1
    served = [r for r in await _rows() if r.status == "ok" and r.native_amount < 120]
    assert served == [], "a refused socket must not record served audio"


async def test_a_disconnect_without_a_flush_still_records(_stub_engine):
    """Audio sent and then abandoned was still processed by the provider."""
    await init_db()
    quota_store.invalidate()
    client = TestClient(app)
    with client.websocket_connect(
        "/v1/stt/stream?engine=stub-stream-stt&sample_rate=16000&denoise=false&vad=false"
    ) as ws:
        assert ws.receive_json()["event_type"] == "session_started"
        for _ in range(4):
            ws.send_bytes(_FRAME)
        # No flush/end: simulate a bare disconnect ourselves, then give the
        # server's cleanup a moment to finish recording. Starlette's
        # WebSocketTestSession.__exit__ sends the disconnect message and then
        # calls cs.cancel() on the app task almost immediately after, with no
        # wait for the app to process it -- racing the finally block's
        # record_usage write. Triggering the disconnect explicitly and
        # polling here (before the `with` block's own teardown does the same
        # thing, harder) gives the handler a real chance to finish before
        # anything cancels it.
        ws.close()
        for _ in range(25):
            if await _rows():
                break
            await asyncio.sleep(0.02)

    rows = await _rows()
    assert len(rows) == 1
    assert abs(rows[0].native_amount - 0.4) < 1e-6


async def test_a_zero_sample_rate_does_not_crash_the_stream(_stub_engine):
    """sample_rate is a query param; metering's len(frame) / 2 / sample_rate
    must not turn a bogus value into a ZeroDivisionError that crashes the
    socket -- metering must never break the stream it's measuring."""
    await init_db()
    quota_store.invalidate()
    client = TestClient(app)
    with client.websocket_connect(
        "/v1/stt/stream?engine=stub-stream-stt&sample_rate=0&denoise=false&vad=false"
    ) as ws:
        assert ws.receive_json()["event_type"] == "session_started"
        ws.send_bytes(_FRAME)
        ws.send_text(json.dumps({"type": "end"}))
        for _ in range(5):
            if ws.receive_json()["event_type"] == "done":
                break
