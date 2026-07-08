import json
import pytest
from fastapi.testclient import TestClient
from app.core.audio import pcm16_to_wav_bytes
from app.core.settings import settings
from app.main import app
from app.schemas.stt import STTResult
from app.schemas.tts import TTSResult
from app.services.artifacts import artifact_store
from app.services.profiles.models import Profile, SessionConfig
from app.services.profiles.store import ProfileStore
from app.services.conversation.lugo_frame import LUGO_FRAME_OPUS
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service


class _StubSTT(STTProvider):
    name = "stub-lugo-stt"
    async def transcribe_bytes(self, audio_bytes, language=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chao", is_final=True)


class _StubTTS(TTSProvider):
    # Lugo forces want_audio=True, so the core actually decodes audio_url to
    # PCM for Opus encoding (unlike the Task-3 core test's want_audio=False
    # stub, which never touches the file). A fake/nonexistent path here would
    # raise FileNotFoundError mid-turn on any machine with real opuslib/libopus
    # available (this dev env has both) and the turn would never reach
    # audio_start/audio_end, hanging the test's receive loop. Persist a real
    # (silent) WAV via the artifact store so the encode path actually succeeds.
    name = "stub-lugo-tts"
    async def synthesize(self, payload) -> TTSResult:
        wav = pcm16_to_wav_bytes(b"\x00\x00" * 2400, sample_rate=24000)  # 100ms silence
        _, audio_url = artifact_store.save_wav(wav)
        return TTSResult(engine=self.name, sample_rate=24000,
                         audio_url=audio_url, duration_seconds=0.1, text=payload.text)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "conversation_llm_base_url", "")
    monkeypatch.setattr(settings, "conversation_stt_engine", "stub-lugo-stt")
    monkeypatch.setattr(settings, "conversation_tts_engine", "stub-lugo-tts")
    stt_service.providers["stub-lugo-stt"] = _StubSTT()
    tts_service.providers["stub-lugo-tts"] = _StubTTS()
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    fresh.upsert(Profile(name="dev", session=SessionConfig(idle_timeout_s=0)))
    monkeypatch.setattr("app.api.routes.lugo.profile_store", fresh)
    yield
    stt_service.providers.pop("stub-lugo-stt", None)
    tts_service.providers.pop("stub-lugo-tts", None)


def test_wakeup_gets_welcome_with_idle_timeout():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "version": 1, "profile": "dev",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        msg = ws.receive_json()
        assert msg["type"] == "welcome"
        assert "session_id" in msg
        assert msg["idle_timeout_s"] == 0


def test_text_turn_yields_stt_and_tts():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": "dev",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        assert ws.receive_json()["type"] == "welcome"
        ws.send_json({"type": "text", "text": "hi"})
        types = []
        first_binary_frame = None
        # Real opus packets are pushed as binary frames interleaved with the
        # JSON wire events (tts start -> N binary packets -> tts stop); skip
        # them here (aside from capturing the first one) since this loop
        # otherwise only asserts on the JSON event sequence.
        for _ in range(20):
            message = ws.receive()
            if message.get("bytes") is not None:
                if first_binary_frame is None:
                    first_binary_frame = message["bytes"]
                continue
            m = json.loads(message["text"])
            types.append((m["type"], m.get("state")))
            if m["type"] == "tts" and m.get("state") == "stop":
                break
        assert ("stt", None) in types
        assert any(t == "tts" for t, _ in types)
        assert first_binary_frame is not None
        assert first_binary_frame[0] == LUGO_FRAME_OPUS


def test_binary_first_frame_errors_not_crashes():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_bytes(b"\x00\x00")
        msg = ws.receive_json()
        assert msg["type"] == "error"


def test_non_json_first_frame_errors():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_text("not json")
        msg = ws.receive_json()
        assert msg["type"] == "error"
