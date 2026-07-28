import asyncio

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.health import EngineHealth
from app.schemas.stt import STTResult
from app.schemas.tts import TTSResult
from app.services.profiles.models import Profile, SttConfig
from app.services.profiles.store import ProfileStore
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service

SR = 16000


class _StubSTT(STTProvider):
    name = "stub-conv"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chào trợ lý", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-conv-tts"

    async def synthesize(self, payload) -> TTSResult:
        return TTSResult(
            engine=self.name, sample_rate=24000, audio_url="/artifacts/x.wav",
            duration_seconds=0.1, text=payload.text,
        )


class _SlowTTS(TTSProvider):
    name = "slow-conv-tts"

    async def synthesize(self, payload) -> TTSResult:
        await asyncio.sleep(0.5)  # window for barge-in
        return TTSResult(
            engine=self.name, sample_rate=24000, audio_url="/artifacts/x.wav",
            duration_seconds=0.1, text=payload.text,
        )


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

    async def _ok_health(stt_engine, stt_model, tts_engine, tts_model):
        return (
            EngineHealth(engine=stt_engine, status="ok"),
            EngineHealth(engine=tts_engine, status="ok"),
        )

    # These stub engine names aren't recognized by stt_service/tts_service's
    # real engine-listing logic, so the Task 7 health gate's
    # check_resolved_engines() would KeyError trying to look them up. Stub the
    # gate out -- this file tests turn/barge-in behavior, not the gate.
    monkeypatch.setattr("app.api.routes.conversation.check_resolved_engines", _ok_health)

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
    """Read the next event, transparently skipping "engines_ready" — it fires
    asynchronously whenever the engine finishes cold-loading and can land at any
    point in the stream, same as a real client (which handles it by name, not
    by position) would treat it."""
    while True:
        ev = ws.receive_json()
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
        for _ in range(30):
            ev = ws.receive_json()
            if ev["event"] == "engines_ready":
                continue
            events.append(ev)
            if ev["event"] == "turn_done":
                break

        types = [e["event"] for e in events]
        assert "speech_end" in types
        assert "user_transcript" in types
        assert "response_text" in types
        assert "audio_chunk" in types
        assert types[-1] == "turn_done"

        transcript = next(e for e in events if e["event"] == "user_transcript")
        assert transcript["text"] == "xin chào trợ lý"
        chunk = next(e for e in events if e["event"] == "audio_chunk")
        assert chunk["audio_url"].startswith("/artifacts/")


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
        for _ in range(12):
            ev = ws.receive_json()["event"]
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
