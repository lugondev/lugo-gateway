import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.audio import pcm16_to_wav_bytes
from app.core.settings import settings
from app.main import app
from app.schemas.stt import STTResult
from app.services.auth.users import user_store
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service


def _silence_wav(ms: int = 100, sr: int = 24000) -> bytes:
    n = int(sr * ms / 1000)
    return pcm16_to_wav_bytes(b"\x00\x00" * n, sample_rate=sr)


class _StubSTT(STTProvider):
    name = "stub-lh-cutoff-stt"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-lh-cutoff-tts"

    async def synthesize(self, payload):  # pragma: no cover - unused; render_audio is the seam now
        raise NotImplementedError("this stub only exercises render_audio()")

    async def render_audio(self, payload) -> tuple[bytes, str]:
        return _silence_wav(), "audio/wav"


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    # conversation_llm_base_url now lives on system_config_store (Task 3), not
    # Settings; the module-level conftest._hermetic fixture already zeroes it.
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    monkeypatch.setattr("app.api.routes.livehost._IDENTITY_RECHECK_INTERVAL_S", 0.05, raising=False)
    stt_service.providers["stub-lh-cutoff-stt"] = _StubSTT()
    tts_service.providers["stub-lh-cutoff-tts"] = _StubTTS()
    yield
    stt_service.providers.pop("stub-lh-cutoff-stt", None)
    tts_service.providers.pop("stub-lh-cutoff-tts", None)
    monkeypatch.setattr(settings, "admin_password", "")


def test_disabled_user_connection_is_closed_within_recheck_interval():
    client = TestClient(app)
    client.post("/api/auth/signup", json={"username": "toan", "password": "pw"})
    client.post("/api/auth/login", json={"username": "toan", "password": "pw"})
    user = asyncio.run(user_store.get_by_username("toan"))

    url = "/v1/livehost/stream?stt_engine=stub-lh-cutoff-stt&tts_engine=stub-lh-cutoff-tts"
    with client.websocket_connect(url) as ws:
        asyncio.run(user_store.set_fields(user.id, disabled=True))
        # receive_json() (unlike the raw receive()) raises once it sees the
        # server's close frame -- raw receive() would just hand back the
        # close message as a plain dict and the *next* call would block
        # forever, since the client-side stream never sees another message.
        with pytest.raises(Exception):  # noqa: B017 -- server closes with 4401
            for _ in range(50):
                ws.receive_json()
