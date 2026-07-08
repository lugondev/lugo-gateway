import pytest
from fastapi.testclient import TestClient
from app.core.settings import settings
from app.main import app
from app.schemas.stt import STTResult
from app.services.profiles.models import Profile, SessionConfig
from app.services.profiles.store import ProfileStore
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service


class _StubSTT(STTProvider):
    name = "stub-idle-stt"
    async def transcribe_bytes(self, audio_bytes, language=None) -> STTResult:
        return STTResult(engine=self.name, text="", is_final=True)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "conversation_llm_base_url", "")
    monkeypatch.setattr(settings, "conversation_stt_engine", "stub-idle-stt")
    stt_service.providers["stub-idle-stt"] = _StubSTT()
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    fresh.upsert(Profile(name="fast", session=SessionConfig(idle_timeout_s=1)))
    monkeypatch.setattr("app.api.routes.lugo.profile_store", fresh)
    # Make the watchdog tick fast so the test is quick.
    monkeypatch.setattr("app.api.routes.lugo._IDLE_TICK_S", 0.1, raising=False)
    yield
    stt_service.providers.pop("stub-idle-stt", None)


def test_idle_timeout_emits_goodbye():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": "fast",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        assert ws.receive_json()["type"] == "welcome"
        # say nothing; within ~1s the server should give up
        msg = ws.receive_json()
        assert msg["type"] == "goodbye"
        assert msg["reason"] == "idle_timeout"
