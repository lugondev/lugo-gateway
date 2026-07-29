"""Unified gateway over /v1/conversation/stream: text/audio in -> text/audio out."""

import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.core.audio import pcm16_to_wav_bytes
from app.main import app
from app.schemas.stt import STTResult
from app.schemas.tts import TTSRequest
from app.services.profiles.models import Profile, SttConfig
from app.services.profiles.store import ProfileStore
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import RenderingTTSProvider
from app.services.tts.service import tts_service

SR = 16000


class _StubSTT(STTProvider):
    name = "stub-gw"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chào", is_final=True)


class _StubTTS(RenderingTTSProvider):
    """A real RenderingTTSProvider so the Opus path (which calls render_wav(),
    never synthesize()) gets real WAV bytes to encode instead of a fabricated
    artifact URL that nothing wrote. synthesize() is inherited unchanged, so
    URL-mode tests below still get a real artifact_store-backed audio_url."""

    name = "stub-gw-tts"
    sample_rate = 24000

    async def _render_wav(self, payload: TTSRequest) -> bytes:
        n = int(self.sample_rate * 100 / 1000)  # 100ms of silence
        return pcm16_to_wav_bytes(b"\x00\x00" * n, sample_rate=self.sample_rate)


@pytest.fixture(autouse=True)
def _stub(monkeypatch, tmp_path):
    # conversation_llm_base_url now lives on system_config_store; the
    # module-level conftest._hermetic fixture already zeroes it (echo
    # responder).
    stt_service.providers["stub-gw"] = _StubSTT()
    tts_service.providers["stub-gw-tts"] = _StubTTS()

    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    fresh_profiles.upsert(Profile(name="p-gw", stt=SttConfig(engine="stub-gw")))
    monkeypatch.setattr("app.api.routes.conversation.profile_store", fresh_profiles)

    from app.services import system_config as sc_mod

    fresh_config = sc_mod.SystemConfigStore(str(tmp_path / "system_config.json"))
    fresh_config.set(
        fresh_config.get().model_copy(
            update={"engines": fresh_config.get().engines.model_copy(update={"default_tts_engine": "stub-gw-tts"})}
        )
    )
    monkeypatch.setattr("app.api.routes.conversation.system_config_store", fresh_config)
    monkeypatch.setattr(sc_mod, "system_config_store", fresh_config)

    yield fresh_profiles, fresh_config

    stt_service.providers.pop("stub-gw", None)
    tts_service.providers.pop("stub-gw-tts", None)


def _loud(ms):
    n = int(SR * ms / 1000)
    return (np.full(n, 0.2, dtype=np.float32) * 32767).astype("<i2").tobytes()


def _drain(ws, stop="turn_done", n=40):
    """Read JSON events, transparently skipping binary reply-audio frames
    (audio_out defaults to wav now, so a WAV frame rides between audio_start
    and audio_end for any test that requests audio output without pinning
    audio_out=opus) -- but COUNTING them and returning the count, so a stray
    binary frame in a text-only turn isn't invisible to the caller's
    assertions. `n` bounds JSON events only: a skipped binary frame must not
    burn out of the same budget, or an audio-heavy turn could exhaust it
    before the JSON event this is actually waiting for arrives."""
    evs = []
    binary_frames = 0
    json_count = 0
    while json_count < n:
        msg = ws.receive()
        if msg.get("bytes") is not None:
            binary_frames += 1
            continue
        json_count += 1
        e = json.loads(msg["text"])
        evs.append(e)
        if e["event"] == stop:
            break
    return evs, binary_frames


def test_text_to_text():
    c = TestClient(app)
    with c.websocket_connect("/v1/conversation/stream?profile=p-gw&output=text") as ws:
        started = ws.receive_json()
        assert started["event"] == "session_started" and started["output"] == ["text"]
        ws.send_json({"type": "text", "text": "xin chào"})
        evs, binary_frames = _drain(ws)
        types = [e["event"] for e in evs]
        assert "user_transcript" in types and "response_text" in types
        assert "audio_start" not in types and "audio_end" not in types  # text-only -> no TTS
        assert binary_frames == 0  # and no stray WAV frame either
        assert next(e for e in evs if e["event"] == "user_transcript")["text"] == "xin chào"


def test_text_to_audio_wav():
    c = TestClient(app)
    with c.websocket_connect("/v1/conversation/stream?profile=p-gw&output=audio") as ws:
        assert ws.receive_json()["event"] == "session_started"
        ws.send_json({"type": "text", "text": "xin chào"})
        evs, binary_frames = _drain(ws)
        types = [e["event"] for e in evs]
        assert "audio_start" in types and "audio_end" in types
        assert next(e for e in evs if e["event"] == "audio_start")["codec"] == "wav"
        assert binary_frames >= 1  # the WAV frame itself actually arrived
        assert "response_text" not in types  # audio-only -> no text events


def _opus_ok():
    # Route through app.core.opus.opus_available() rather than a bare
    # `import opuslib`: opuslib locates libopus via ctypes.util.find_library,
    # which needs app.core.opus._ensure_libopus_findable()'s shim to succeed
    # on this host (see that module's docstring). A bare import only works if
    # some other, already-collected module ran the shim first -- making
    # whether this test runs or skips depend on collection order (it always
    # runs under `pytest tests/unit tests/integration`, since a unit test
    # imports opus_available() first, but always skips under plain `pytest`,
    # which collects tests/integration/ first).
    from app.core.opus import opus_available

    if not opus_available():
        return False
    import opuslib

    try:
        opuslib.Encoder(24000, 1, opuslib.APPLICATION_VOIP)
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _opus_ok(), reason="libopus not loadable")
def test_text_to_opus_frames(_stub):
    # Verify Opus framing only; disable real-time pacing so the test doesn't sleep
    # through the reply audio. Pacing schedule is unit-tested.
    # conversation_opus_pace lives on system_config_store's `conversation`
    # group, not Settings. Write through the same fresh store the `_stub`
    # fixture already pointed app.api.routes.conversation at (rather than
    # monkeypatching the original module-level singleton's .get -- that
    # singleton is no longer what the route reads once `_stub` has rebound
    # it to a fresh, isolated store; see test_conversation_tts_profile.py's
    # `_local_hermetic` for the same dual-binding gotcha).
    _profiles, fresh_config = _stub
    fresh_config.set(
        fresh_config.get().model_copy(
            update={"conversation": fresh_config.get().conversation.model_copy(update={"conversation_opus_pace": False})}
        )
    )
    c = TestClient(app)
    url = "/v1/conversation/stream?profile=p-gw&output=audio&audio_out=opus&output_sample_rate=24000"
    with c.websocket_connect(url) as ws:
        started = ws.receive_json()
        assert started["audio_out"] == "opus" and started["output_sample_rate"] == 24000
        ws.send_json({"type": "text", "text": "xin chào"})
        saw_start = saw_end = False
        binary_frames = 0
        for _ in range(200):
            msg = ws.receive()
            if "bytes" in msg and msg["bytes"] is not None:
                binary_frames += 1
                continue
            ev = json.loads(msg["text"])["event"]
            if ev == "audio_start":
                saw_start = True
            elif ev == "audio_end":
                saw_end = True
            elif ev == "turn_done":
                break
        assert saw_start and saw_end and binary_frames > 0


def test_audio_to_text():
    c = TestClient(app)
    url = "/v1/conversation/stream?profile=p-gw&output=text&sample_rate=16000"
    with c.websocket_connect(url) as ws:
        assert ws.receive_json()["event"] == "session_started"
        ws.send_bytes(_loud(500))
        assert ws.receive_json()["event"] == "speech_start"
        ws.send_bytes(_loud(400))
        ws.send_bytes((b"\x00\x00") * int(SR * 1.0))  # silence -> endpoint
        evs, binary_frames = _drain(ws)
        types = [e["event"] for e in evs]
        assert "user_transcript" in types and "response_text" in types
        assert "audio_start" not in types and "audio_end" not in types
        assert binary_frames == 0
