import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.settings import settings
from app.main import app
from app.schemas.stt import STTResult
from app.schemas.tts import TTSResult
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service


class _StubSTT(STTProvider):
    name = "stub-ws-auth-stt"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="ok", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-ws-auth-tts"

    async def synthesize(self, payload) -> TTSResult:
        return TTSResult(
            engine=self.name, sample_rate=24000, audio_url="/artifacts/x.wav",
            duration_seconds=0.1, text=payload.text,
        )


@pytest.fixture(autouse=True)
def _register_stub_engines():
    stt_service.providers["stub-ws-auth-stt"] = _StubSTT()
    tts_service.providers["stub-ws-auth-tts"] = _StubTTS()
    yield
    stt_service.providers.pop("stub-ws-auth-stt", None)
    tts_service.providers.pop("stub-ws-auth-tts", None)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    monkeypatch.setattr(settings, "device_auth_token", "d3vice-secret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")
    monkeypatch.setattr(settings, "device_auth_token", "")


# (path, query string appended for the "accepts" tests only — irrelevant for the
# rejection test, since auth is checked before any query param is read)
ROUTES = [
    ("/v1/conversation/stream", "stt_engine=stub-ws-auth-stt&tts_engine=stub-ws-auth-tts"),
    ("/v1/stt/stream", "engine=vosk"),
    ("/v1/livehost/stream", "stt_engine=stub-ws-auth-stt&tts_engine=stub-ws-auth-tts"),
]


@pytest.mark.parametrize("path,query", ROUTES)
def test_ws_rejects_unauthenticated_connection(client, _with_password, path, query):
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(path):
            pass
    assert exc_info.value.code == 4401


@pytest.mark.parametrize("path,query", ROUTES)
def test_ws_accepts_valid_device_token(client, _with_password, path, query):
    with client.websocket_connect(f"{path}?{query}&device_token=d3vice-secret") as ws:
        first = ws.receive_json()
        assert first  # session_started — past the auth gate


@pytest.mark.parametrize("path,query", ROUTES)
def test_ws_accepts_valid_browser_cookie(client, _with_password, path, query):
    client.post("/api/auth/signup", json={"username": "toan", "password": "s3cret"})
    login = client.post("/api/auth/login", json={"username": "toan", "password": "s3cret"})
    assert login.status_code == 200
    with client.websocket_connect(f"{path}?{query}") as ws:
        first = ws.receive_json()
        assert first


@pytest.mark.parametrize("path,query", ROUTES)
def test_ws_rejects_disabled_user_cookie(client, _with_password, path, query, monkeypatch):
    import asyncio

    from app.services.auth.users import user_store

    client.post("/api/auth/signup", json={"username": "toan", "password": "pw"})
    login = client.post("/api/auth/login", json={"username": "toan", "password": "pw"})
    assert login.status_code == 200
    user = asyncio.run(user_store.get_by_username("toan"))
    asyncio.run(user_store.set_fields(user.id, disabled=True))

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"{path}?{query}"):
            pass
    assert exc_info.value.code == 4401
