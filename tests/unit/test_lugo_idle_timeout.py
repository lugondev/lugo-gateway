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


def test_idle_countdown_starts_after_the_bot_finishes(monkeypatch):
    """The idle countdown must start when the bot FINISHES replying, not before —
    a slow turn's think/response time must NOT be counted toward idle.

    Drives a turn that takes ~1.5s (> idle_timeout_s=1) with the client sitting
    silent (no uplink), then measures the gap between the turn's tts-stop and the
    idle goodbye. If the think time were counted (the bug), goodbye fires almost
    immediately after tts-stop (~one 0.1s tick); with the fix it fires ~idle (1s)
    later. Asserting the gap is close to idle catches the regression."""
    import asyncio
    import json as _json
    import time as _time
    from app.core.audio import pcm16_to_wav_bytes
    from app.schemas.tts import TTSResult
    from app.services.artifacts import artifact_store
    from app.services.tts.base import TTSProvider
    from app.services.tts.service import tts_service

    class _SlowTTS(TTSProvider):
        name = "stub-slow-tts"
        async def synthesize(self, payload) -> TTSResult:
            await asyncio.sleep(1.5)  # turn takes longer than idle_timeout_s=1
            wav = pcm16_to_wav_bytes(b"\x00\x00" * 2400, sample_rate=24000)
            _, url = artifact_store.save_wav(wav)
            return TTSResult(engine=self.name, sample_rate=24000, audio_url=url,
                             duration_seconds=0.1, text=payload.text)

    monkeypatch.setattr(settings, "conversation_tts_engine", "stub-slow-tts")
    tts_service.providers["stub-slow-tts"] = _SlowTTS()
    try:
        with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
            ws.send_json({"type": "wakeup", "profile": "fast",
                          "audio_params": {"format": "opus", "sample_rate": 16000}})
            assert ws.receive_json()["type"] == "welcome"
            ws.send_json({"type": "text", "text": "hi"})  # echo responder -> slow TTS
            # Consume the turn up to and including its tts stop.
            for _ in range(60):
                m = ws.receive()
                if m.get("bytes") is not None:
                    continue
                d = _json.loads(m["text"])
                if d.get("type") == "tts" and d.get("state") == "stop":
                    break
            bot_done = _time.monotonic()
            # Now sit silent and wait for the idle goodbye; time how long it takes.
            for _ in range(60):
                m = ws.receive()
                if m.get("bytes") is not None:
                    continue
                d = _json.loads(m["text"])
                if d.get("type") == "goodbye":
                    break
            gap = _time.monotonic() - bot_done
            assert d["type"] == "goodbye"
            # Countdown restarted at turn end -> ~idle (1s). The bug fires ~immediately.
            assert gap >= 0.6, f"idle fired {gap:.2f}s after the bot finished (think time was counted)"
    finally:
        tts_service.providers.pop("stub-slow-tts", None)
