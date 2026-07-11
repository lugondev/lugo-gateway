import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.schemas.stt import STTResult
from app.schemas.tts import TTSResult
from app.services.livehost.registry import livehost_registry
from app.services.livehost.schemas import SocialEvent
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service


class _StubSTT(STTProvider):
    name = "stub-livehost-social"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-livehost-social-tts"

    async def synthesize(self, payload) -> TTSResult:
        return TTSResult(
            engine=self.name, sample_rate=24000, audio_url="/artifacts/x.wav",
            duration_seconds=0.05, text=payload.text,
        )


@pytest.fixture(autouse=True)
def _register_stub(monkeypatch):
    monkeypatch.setattr(settings, "conversation_llm_base_url", "")
    monkeypatch.setattr(settings, "livehost_individual_threshold", 5)
    stt_service.providers["stub-livehost-social"] = _StubSTT()
    tts_service.providers["stub-livehost-social-tts"] = _StubTTS()
    yield
    stt_service.providers.pop("stub-livehost-social", None)
    tts_service.providers.pop("stub-livehost-social-tts", None)


def test_social_event_triggers_reply_when_streamer_silent():
    client = TestClient(app)
    url = "/v1/livehost/stream?stt_engine=stub-livehost-social&tts_engine=stub-livehost-social-tts"
    with client.websocket_connect(url) as ws:
        started = ws.receive_json()
        session_id = started["session_id"]

        session = livehost_registry.get(session_id)
        assert session is not None
        session.scheduler.enqueue(
            SocialEvent(id="e1", kind="comment", user_id="u1", user_name="Bao", text="hello!", timestamp=1.0)
        )

        events = []
        for _ in range(20):
            ev = ws.receive_json()
            events.append(ev)
            if ev["event"] == "turn_done":
                break

        kinds = [e["event"] for e in events]
        assert "social_reply" in kinds
        assert "audio_chunk" in kinds


class _FakeTikTokClient:
    """Stands in for TikTokLiveClientAdapter so this test never touches the
    real network — it exercises the connect/disconnect/status wiring only."""

    def __init__(self, unique_id: str) -> None:
        self.unique_id = unique_id

    async def connect(self) -> None:
        await asyncio.sleep(3600)  # "stays live" for the duration of the test

    def events(self):
        async def _gen():
            await asyncio.sleep(3600)
            yield None  # pragma: no cover - unreachable, keeps this an async generator

        return _gen()

    async def close(self) -> None:
        pass


def test_connect_disconnect_status_endpoints(monkeypatch):
    monkeypatch.setattr(
        "app.api.routes.livehost._default_tiktok_client_factory",
        lambda unique_id: _FakeTikTokClient(unique_id),
    )
    client = TestClient(app)
    with client.websocket_connect("/v1/livehost/stream?stt_engine=stub-livehost-social&tts_engine=stub-livehost-social-tts") as ws:
        session_id = ws.receive_json()["session_id"]

        status = client.get(f"/v1/livehost/{session_id}/status").json()
        assert status["data"]["state"] == "idle"

        resp = client.post(f"/v1/livehost/{session_id}/connect", json={"unique_id": "some_streamer"})
        assert resp.status_code == 200
        assert resp.json()["data"]["unique_id"] == "some_streamer"

        client.post(f"/v1/livehost/{session_id}/disconnect")
        status = client.get(f"/v1/livehost/{session_id}/status").json()
        assert status["data"]["state"] == "idle"


def test_status_for_unknown_session_is_404():
    client = TestClient(app)
    resp = client.get("/v1/livehost/does-not-exist/status")
    assert resp.status_code == 404
