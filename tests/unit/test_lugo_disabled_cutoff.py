import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.schemas.stt import STTResult
from app.services.auth.devices import device_store
from app.services.auth.users import user_store
from app.services.profiles.models import Profile, SessionConfig
from app.services.profiles.store import ProfileStore
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service


class _StubSTT(STTProvider):
    name = "stub-lugo-cutoff-stt"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="", is_final=True)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "conversation_llm_base_url", "")
    monkeypatch.setattr(settings, "conversation_stt_engine", "stub-lugo-cutoff-stt")
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    monkeypatch.setattr(settings, "conversation_goodbye_text", "")
    stt_service.providers["stub-lugo-cutoff-stt"] = _StubSTT()
    # idle_timeout_s huge so only the identity re-check can fire in this test.
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    fresh.upsert(Profile(name="fast", session=SessionConfig(idle_timeout_s=3600)))
    monkeypatch.setattr("app.api.routes.lugo.profile_store", fresh)
    monkeypatch.setattr("app.api.routes.lugo._IDLE_TICK_S", 0.05, raising=False)
    monkeypatch.setattr("app.api.routes.lugo._IDENTITY_RECHECK_INTERVAL_S", 0.05, raising=False)
    yield
    stt_service.providers.pop("stub-lugo-cutoff-stt", None)
    monkeypatch.setattr(settings, "admin_password", "")


def test_disabled_owner_cuts_off_paired_device():
    import asyncio

    user = asyncio.run(user_store.create("toan", "pw"))
    device, raw_token = asyncio.run(device_store.create(user["id"], "ESP32", "AA:BB:CC"))

    client = TestClient(app)
    with client.websocket_connect(f"/v1/lugo/stream?device_token={raw_token}") as ws:
        ws.send_json({"type": "wakeup", "profile": "fast",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        assert ws.receive_json()["type"] == "welcome"
        asyncio.run(user_store.set_fields(user["id"], disabled=True))
        msg = ws.receive_json()
        assert msg["type"] == "goodbye"
        assert msg["reason"] == "account_disabled"
