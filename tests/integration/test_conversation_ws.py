import asyncio
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.core.audio import pcm16_to_wav_bytes
from app.main import app
from app.schemas.stt import STTResult
from app.services.profiles.models import Profile, SttConfig
from app.services.profiles.store import ProfileStore
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service

SR = 16000


def _silence_wav(ms: int = 100, sr: int = 24000) -> bytes:
    n = int(sr * ms / 1000)
    return pcm16_to_wav_bytes(b"\x00\x00" * n, sample_rate=sr)


class _StubSTT(STTProvider):
    name = "stub-conv"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chào trợ lý", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-conv-tts"

    async def synthesize(self, payload):  # pragma: no cover - unused; render_audio is the seam now
        raise NotImplementedError("this stub only exercises render_audio()")

    async def render_audio(self, payload) -> tuple[bytes, str]:
        return _silence_wav(), "audio/wav"


class _SlowTTS(TTSProvider):
    name = "slow-conv-tts"

    async def synthesize(self, payload):  # pragma: no cover - unused; render_audio is the seam now
        raise NotImplementedError("this stub only exercises render_audio()")

    async def render_audio(self, payload) -> tuple[bytes, str]:
        await asyncio.sleep(0.5)  # window for barge-in
        return _silence_wav(), "audio/wav"


@pytest.fixture(autouse=True)
def _register_stub(monkeypatch, tmp_path):
    # Keep the test hermetic regardless of .env: stub TTS + built-in echo responder
    # (no external Ollama / real model calls). conversation_llm_base_url now
    # lives on system_config_store; conftest._hermetic already zeroes it.
    stt_service.providers["stub-conv"] = _StubSTT()
    tts_service.providers["stub-conv-tts"] = _StubTTS()
    tts_service.providers["slow-conv-tts"] = _SlowTTS()

    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    monkeypatch.setattr("app.api.routes.conversation.profile_store", fresh_profiles)

    yield fresh_profiles

    stt_service.providers.pop("stub-conv", None)
    tts_service.providers.pop("stub-conv-tts", None)
    tts_service.providers.pop("slow-conv-tts", None)


def _loud(ms: int) -> bytes:
    n = int(SR * ms / 1000)
    return (np.full(n, 0.2, dtype=np.float32) * 32767).astype("<i2").tobytes()


def _silence(ms: int) -> bytes:
    return (b"\x00\x00") * int(SR * ms / 1000)


def _next_event(ws) -> dict:
    """Read the next JSON event, transparently skipping "engines_ready" (fires
    asynchronously whenever the engine finishes cold-loading and can land at
    any point in the stream, same as a real client -- which handles it by
    name, not by position -- would treat it) and binary reply-audio frames
    (audio_out defaults to wav now, so a WAV frame rides between audio_start
    and audio_end)."""
    while True:
        msg = ws.receive()
        if msg.get("bytes") is not None:
            continue
        ev = json.loads(msg["text"])
        if ev["event"] != "engines_ready":
            return ev


def _set_default_tts(monkeypatch, tmp_path, tts_engine):
    from app.services import system_config as sc_mod

    fresh = sc_mod.SystemConfigStore(str(tmp_path / "system_config.json"))
    fresh.set(
        fresh.get().model_copy(
            update={"engines": fresh.get().engines.model_copy(update={"default_tts_engine": tts_engine})}
        )
    )
    monkeypatch.setattr("app.api.routes.conversation.system_config_store", fresh)
    monkeypatch.setattr(sc_mod, "system_config_store", fresh)


def test_conversation_turn_end_to_end(_register_stub, monkeypatch, tmp_path):
    _register_stub.upsert(Profile(name="p1", stt=SttConfig(engine="stub-conv")))
    _set_default_tts(monkeypatch, tmp_path, "stub-conv-tts")
    client = TestClient(app)
    url = "/v1/conversation/stream?profile=p1&sample_rate=16000"
    with client.websocket_connect(url) as ws:
        assert ws.receive_json()["event"] == "session_started"

        ws.send_bytes(_loud(500))
        assert _next_event(ws)["event"] == "speech_start"
        ws.send_bytes(_loud(400))
        ws.send_bytes(_silence(500))
        ws.send_bytes(_silence(500))  # crosses 700ms silence -> endpoint

        events = []
        audio_frames = []
        # 30 bounds JSON events only -- a skipped binary WAV frame must not
        # burn out of the same budget as the JSON events this loop is
        # actually waiting on.
        while len(events) < 30:
            msg = ws.receive()
            if msg.get("bytes") is not None:
                audio_frames.append(msg["bytes"])
                continue
            ev = json.loads(msg["text"])
            if ev["event"] == "engines_ready":
                continue
            events.append(ev)
            if ev["event"] == "turn_done":
                break

        types = [e["event"] for e in events]
        assert "speech_end" in types
        assert "user_transcript" in types
        assert "response_text" in types
        assert "audio_start" in types
        assert "audio_end" in types
        assert types[-1] == "turn_done"

        transcript = next(e for e in events if e["event"] == "user_transcript")
        assert transcript["text"] == "xin chào trợ lý"
        start = next(e for e in events if e["event"] == "audio_start")
        assert start["codec"] == "wav"
        assert audio_frames and audio_frames[0][:4] == b"RIFF"


def test_conversation_barge_in_aborts_turn(_register_stub, monkeypatch, tmp_path):
    _register_stub.upsert(Profile(name="p2", stt=SttConfig(engine="stub-conv")))
    _set_default_tts(monkeypatch, tmp_path, "slow-conv-tts")
    client = TestClient(app)
    url = "/v1/conversation/stream?profile=p2&sample_rate=16000"
    with client.websocket_connect(url) as ws:
        assert ws.receive_json()["event"] == "session_started"
        ws.send_bytes(_loud(500))
        assert _next_event(ws)["event"] == "speech_start"
        ws.send_bytes(_loud(400))
        ws.send_bytes(_silence(500))
        ws.send_bytes(_silence(500))  # endpoint -> turn starts (slow TTS)
        ws.send_bytes(_loud(500))  # barge-in while assistant is synthesizing

        seen = []
        # 12 bounds JSON events only -- reply-audio binary frames from the
        # slow TTS must not crowd out `aborted` and make this flaky.
        while len(seen) < 12:
            msg = ws.receive()
            if msg.get("bytes") is not None:
                continue
            ev = json.loads(msg["text"])["event"]
            if ev == "engines_ready":
                continue
            seen.append(ev)
            if ev == "aborted":
                break
        assert "aborted" in seen  # the in-progress turn was cancelled


def test_conversation_unknown_engine_errors(_register_stub):
    _register_stub.upsert(Profile(name="p3", stt=SttConfig(engine="nope")))
    client = TestClient(app)
    with client.websocket_connect("/v1/conversation/stream?profile=p3") as ws:
        assert ws.receive_json()["event"] == "error"


def test_conversation_llm_config_set_and_reset():
    # No manual cleanup needed: the LLM config now lives in a Model Registry
    # DB row, and conftest's per-test tmp DB already isolates this from other
    # tests (unlike the old in-memory globals, which needed an explicit reset).
    client = TestClient(app)
    # Default (hermetic): no base url -> echo responder.
    body = client.get("/v1/conversation/llm").json()["data"]
    assert body["responder"] == "echo"

    # Point at an online OpenAI-compatible endpoint.
    body = client.post(
        "/v1/conversation/llm",
        json={"base_url": "https://api.example.com/v1", "api_key": "sk-x", "model": "gpt-test"},
    ).json()["data"]
    assert body["responder"] == "llm"
    assert body["base_url"] == "https://api.example.com/v1"
    assert body["model"] == "gpt-test"
    assert body["api_key_set"] is True  # key is never echoed back, only a flag

    # Revert.
    body = client.post("/v1/conversation/llm/reset").json()["data"]
    assert body["responder"] == "echo"
