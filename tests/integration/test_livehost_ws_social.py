import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.core.audio import pcm16_to_wav_bytes
from app.core.settings import settings
from app.main import app
from app.schemas.stt import STTResult
from app.services.livehost.registry import livehost_registry
from app.services.livehost.schemas import SocialEvent
from app.services.profiles.models import Profile, SttConfig
from app.services.profiles.store import ProfileStore
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service


def _silence_wav(ms: int = 100, sr: int = 24000) -> bytes:
    n = int(sr * ms / 1000)
    return pcm16_to_wav_bytes(b"\x00\x00" * n, sample_rate=sr)


class _StubSTT(STTProvider):
    name = "stub-livehost-social"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-livehost-social-tts"

    def __init__(self) -> None:
        self.calls: list = []

    async def synthesize(self, payload):  # pragma: no cover - unused; render_audio is the seam now
        raise NotImplementedError("this stub only exercises render_audio()")

    async def render_audio(self, payload) -> tuple[bytes, str]:
        self.calls.append(payload)
        return _silence_wav(), "audio/wav"


@pytest.fixture(autouse=True)
def _register_stub(monkeypatch, tmp_path):
    # conversation_llm_base_url now lives on system_config_store; the
    # module-level conftest._hermetic fixture already zeroes it.
    monkeypatch.setattr(settings, "livehost_individual_threshold", 5)
    stt_service.providers["stub-livehost-social"] = _StubSTT()
    stub_tts = _StubTTS()
    tts_service.providers["stub-livehost-social-tts"] = stub_tts

    # livehost.py resolves stt_engine from the profile (else server default)
    # and tts_engine from engines.default_tts_engine -- neither reads a
    # query-param engine override any more (Task 7). Pin both here, dual-
    # patching app.api.routes.livehost.system_config_store (the module's own
    # import-time binding) and app.services.system_config.system_config_store
    # (what resolve_stt() re-imports at call time), same pattern already used
    # in test_livehost_ws_voice.py/test_livehost_tts_profile.py -- otherwise
    # this test silently falls through to the real ambient default_tts_engine
    # (omnivoice) instead of the registered stub.
    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    fresh_profiles.upsert(Profile(name="p-social", stt=SttConfig(engine="stub-livehost-social")))
    monkeypatch.setattr("app.api.routes.livehost.profile_store", fresh_profiles)

    from app.services import system_config as sc_mod

    fresh_config = sc_mod.SystemConfigStore(str(tmp_path / "system_config.json"))
    fresh_config.set(
        fresh_config.get().model_copy(
            update={
                "engines": fresh_config.get().engines.model_copy(
                    update={"default_tts_engine": "stub-livehost-social-tts"}
                ),
            }
        )
    )
    monkeypatch.setattr("app.api.routes.livehost.system_config_store", fresh_config)
    monkeypatch.setattr(sc_mod, "system_config_store", fresh_config)

    yield stub_tts

    stt_service.providers.pop("stub-livehost-social", None)
    tts_service.providers.pop("stub-livehost-social-tts", None)


def test_social_event_triggers_reply_when_streamer_silent(_register_stub):
    client = TestClient(app)
    url = "/v1/livehost/stream?profile=p-social"
    with client.websocket_connect(url) as ws:
        started = ws.receive_json()
        session_id = started["session_id"]

        session = livehost_registry.get(session_id)
        assert session is not None
        session.scheduler.enqueue(
            SocialEvent(id="e1", kind="comment", user_id="u1", user_name="Bao", text="hello!", timestamp=1.0)
        )

        events = []
        frames = []
        # 20 bounds JSON events only -- a skipped binary WAV frame must not
        # burn out of the same budget as the JSON events this loop is
        # actually waiting on (mirrors test_livehost_ws_voice.py's
        # _drive_voice_turn).
        while len(events) < 20:
            msg = ws.receive()
            if msg.get("bytes") is not None:
                frames.append(msg["bytes"])
                continue
            ev = json.loads(msg["text"])
            events.append(ev)
            if ev["event"] == "turn_done":
                break

        kinds = [e["event"] for e in events]
        assert "social_reply" in kinds
        assert "audio_start" in kinds
        assert "audio_end" in kinds
        assert frames and frames[0][:4] == b"RIFF"

    # Prove this actually exercised the registered stub, not the real
    # ambient default_tts_engine (omnivoice) -- the exact regression this
    # fix addresses.
    assert _register_stub.calls, "TTS provider was never invoked"
    assert _register_stub.calls[0].engine == "stub-livehost-social-tts"


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
