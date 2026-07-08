# tests/unit/test_lugo_barge_in.py
import asyncio
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
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service


class _StubSTT(STTProvider):
    name = "stub-bi-stt"
    async def transcribe_bytes(self, audio_bytes, language=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chao", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-bi-tts"
    async def synthesize(self, payload) -> TTSResult:
        wav = pcm16_to_wav_bytes(b"\x00\x00" * 2400, sample_rate=24000)  # 100ms silence
        _, audio_url = artifact_store.save_wav(wav)
        return TTSResult(engine=self.name, sample_rate=24000,
                         audio_url=audio_url, duration_seconds=0.1, text=payload.text)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "conversation_llm_base_url", "")
    monkeypatch.setattr(settings, "conversation_stt_engine", "stub-bi-stt")
    monkeypatch.setattr(settings, "conversation_tts_engine", "stub-bi-tts")
    stt_service.providers["stub-bi-stt"] = _StubSTT()
    tts_service.providers["stub-bi-tts"] = _StubTTS()
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    fresh.upsert(Profile(name="dev", session=SessionConfig(idle_timeout_s=0)))
    monkeypatch.setattr("app.api.routes.lugo.profile_store", fresh)
    yield
    stt_service.providers.pop("stub-bi-stt", None)
    tts_service.providers.pop("stub-bi-tts", None)


def test_abort_then_still_usable():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": "dev",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        assert ws.receive_json()["type"] == "welcome"
        # abort with no active turn is a safe no-op; connection stays open
        ws.send_json({"type": "abort", "reason": "wake_word_detected"})
        # a subsequent text turn still works -> connection was not closed
        ws.send_json({"type": "text", "text": "hi"})
        seen_stt = False
        seen_tts_stop = False
        # Drain the turn to its terminal state before leaving the `with` block,
        # mirroring test_lugo_stream.py's pattern: exiting early (right after
        # the `stt` frame) leaves in-flight LLM/TTS/opus-encode work running,
        # which hangs the TestClient's blocking portal on teardown.
        for _ in range(20):
            message = ws.receive()
            if message.get("bytes") is not None:
                # Binary downlink opus packets; not JSON, just skip them.
                continue
            m = json.loads(message["text"])
            if m["type"] == "stt":
                seen_stt = True
            if m["type"] == "tts" and m.get("state") == "stop":
                seen_tts_stop = True
                break
        assert seen_stt
        assert seen_tts_stop


def test_abort_emits_tts_stop(monkeypatch):
    # Make TTS slow so the turn is still in-flight when we send abort, so the
    # abort (not the natural turn_done) is what produces the tts stop.
    slow = tts_service.providers["stub-bi-tts"]
    orig = slow.synthesize
    async def slow_synth(payload):
        await asyncio.sleep(0.3)
        return await orig(payload)
    monkeypatch.setattr(slow, "synthesize", slow_synth)

    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": "dev",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        assert ws.receive_json()["type"] == "welcome"
        ws.send_json({"type": "text", "text": "kể một câu chuyện"})
        # Wait until the bot has started speaking (tts start).
        started = False
        for _ in range(30):
            message = ws.receive()
            if message.get("bytes") is not None:
                continue
            m = json.loads(message["text"])
            if m["type"] == "tts" and m["state"] == "start":
                started = True
                break
        assert started
        ws.send_json({"type": "abort", "reason": "user"})
        saw_stop = False
        for _ in range(30):
            message = ws.receive()
            if message.get("bytes") is not None:
                continue
            m = json.loads(message["text"])
            if m["type"] == "tts" and m["state"] == "stop":
                saw_stop = True
                break
        assert saw_stop
