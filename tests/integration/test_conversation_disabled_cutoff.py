import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.schemas.stt import STTResult
from app.services.auth.users import user_store
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service


class _StubSTT(STTProvider):
    name = "stub-cutoff-stt"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="", is_final=True)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setattr(settings, "conversation_llm_base_url", "")
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    monkeypatch.setattr("app.api.routes.conversation._IDENTITY_RECHECK_INTERVAL_S", 0.05, raising=False)
    stt_service.providers["stub-cutoff-stt"] = _StubSTT()
    yield
    stt_service.providers.pop("stub-cutoff-stt", None)
    monkeypatch.setattr(settings, "admin_password", "")


def test_disabled_user_connection_is_closed_within_recheck_interval():
    client = TestClient(app)
    client.post("/api/auth/signup", json={"username": "toan", "password": "pw"})
    client.post("/api/auth/login", json={"username": "toan", "password": "pw"})
    user = asyncio.run(user_store.get_by_username("toan"))

    with client.websocket_connect("/v1/conversation/stream?stt_engine=stub-cutoff-stt") as ws:
        asyncio.run(user_store.set_fields(user.id, disabled=True))
        # receive_json() (unlike the raw receive()) raises WebSocketDisconnect once
        # it sees the server's close frame -- raw receive() would just hand back
        # the close message as a plain dict and the *next* call would block
        # forever, since the client-side stream never sees another message.
        with pytest.raises(Exception):  # noqa: B017 -- server closes with 4401
            for _ in range(50):
                ws.receive_json()
