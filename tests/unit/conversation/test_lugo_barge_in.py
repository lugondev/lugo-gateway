# tests/unit/conversation/test_lugo_barge_in.py
import asyncio
import json
import pytest
from fastapi.testclient import TestClient
from app.core.audio import pcm16_to_wav_bytes
from app.main import app
from app.schemas.stt import STTResult
from app.services.profiles.models import Profile, SessionConfig
from app.services.profiles.store import ProfileStore
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.system_config import system_config_store
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service


def _silence_wav(ms: int = 100, sr: int = 24000) -> bytes:
    n = int(sr * ms / 1000)
    return pcm16_to_wav_bytes(b"\x00\x00" * n, sample_rate=sr)


class _StubSTT(STTProvider):
    name = "stub-bi-stt"
    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chao", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-bi-tts"

    async def render_audio(self, payload) -> tuple[bytes, str]:
        return _silence_wav(), "audio/wav"


@pytest.fixture(autouse=True)
def _local_hermetic(monkeypatch, tmp_path):
    # Named distinctly from conftest.py's `_hermetic` so both autouse fixtures
    # run (a same-named fixture here would shadow, not compose with, the
    # global one -- see conftest.py's _hermetic for what that one handles).
    # default_stt_engine/default_tts_engine live on system_config_store's
    # `engines` group, not Settings. Patch the shared singleton's .get() (not
    # .set()) so this never writes through to the shared config_system DB row.
    _real_get = system_config_store.get

    def _get_with_stub_engines():
        cfg = _real_get()
        return cfg.model_copy(update={
            "engines": cfg.engines.model_copy(update={
                "default_stt_engine": "stub-bi-stt",
                "default_tts_engine": "stub-bi-tts",
            })
        })

    monkeypatch.setattr(system_config_store, "get", _get_with_stub_engines)
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
    orig = slow.render_audio
    async def slow_render_audio(payload):
        await asyncio.sleep(0.3)
        return await orig(payload)
    monkeypatch.setattr(slow, "render_audio", slow_render_audio)

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
