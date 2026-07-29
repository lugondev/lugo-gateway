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
    name = "stub-livehost"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="chao ban", is_final=True)


class _FailingSTT(STTProvider):
    name = "stub-livehost-failing"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        raise RuntimeError("boom")


class _StubTTS(TTSProvider):
    name = "stub-livehost-tts"

    async def synthesize(self, payload):  # pragma: no cover - unused; render_audio is the seam now
        raise NotImplementedError("this stub only exercises render_audio()")

    async def render_audio(self, payload) -> tuple[bytes, str]:
        return _silence_wav(), "audio/wav"


@pytest.fixture(autouse=True)
def _register_stub(monkeypatch, tmp_path):
    # conversation_llm_base_url now lives on system_config_store; conftest's
    # module-level _hermetic fixture already zeroes it.
    stt_service.providers["stub-livehost"] = _StubSTT()
    stt_service.providers["stub-livehost-failing"] = _FailingSTT()
    tts_service.providers["stub-livehost-tts"] = _StubTTS()

    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    monkeypatch.setattr("app.api.routes.livehost.profile_store", fresh_profiles)

    yield fresh_profiles

    stt_service.providers.pop("stub-livehost", None)
    stt_service.providers.pop("stub-livehost-failing", None)
    tts_service.providers.pop("stub-livehost-tts", None)


def _loud(ms: int) -> bytes:
    n = int(SR * ms / 1000)
    return (np.full(n, 0.2, dtype=np.float32) * 32767).astype("<i2").tobytes()


def _silence(ms: int) -> bytes:
    return (b"\x00\x00") * int(SR * ms / 1000)


def _set_default_tts(monkeypatch, tmp_path, tts_engine):
    from app.services import system_config as sc_mod

    fresh = sc_mod.SystemConfigStore(str(tmp_path / "system_config.json"))
    fresh.set(
        fresh.get().model_copy(
            update={"engines": fresh.get().engines.model_copy(update={"default_tts_engine": tts_engine})}
        )
    )
    monkeypatch.setattr("app.api.routes.livehost.system_config_store", fresh)
    monkeypatch.setattr(sc_mod, "system_config_store", fresh)


def _drive_voice_turn(monkeypatch, tmp_path, audio_out="wav"):
    """Registers profile "p1", drives one voice turn end-to-end over the
    livehost WS, and returns (events, frames): events is a list of
    (event_name, payload) tuples in receipt order, frames is the list of raw
    binary WS frames received (reply audio). A skipped binary frame does not
    count against the events budget -- same reasoning as Task 1's
    test_conversation_ws.py, where a naive receive_json()-only loop hangs
    instead of failing cleanly once TTS output stops riding an all-JSON
    audio_chunk event and starts interleaving binary frames.
    """
    import app.api.routes.livehost as livehost_route

    livehost_route.profile_store.upsert(Profile(name="p1", stt=SttConfig(engine="stub-livehost")))
    _set_default_tts(monkeypatch, tmp_path, "stub-livehost-tts")
    client = TestClient(app)
    url = f"/v1/livehost/stream?profile=p1&sample_rate=16000&audio_out={audio_out}"
    events: list[tuple[str, dict]] = []
    frames: list[bytes] = []
    with client.websocket_connect(url) as ws:
        started = ws.receive_json()
        assert started["event"] == "session_started"
        session_id = started["session_id"]

        ws.send_bytes(_loud(500))
        ws.send_bytes(_silence(500))
        ws.send_bytes(_silence(500))

        while len(events) < 20:
            msg = ws.receive()
            if msg.get("bytes") is not None:
                frames.append(msg["bytes"])
                continue
            ev = json.loads(msg["text"])
            events.append((ev["event"], ev))
            if ev["event"] == "turn_done":
                break

    from app.services.livehost.registry import livehost_registry
    assert livehost_registry.get(session_id) is None  # cleaned up on disconnect
    return events, frames


def test_livehost_voice_turn_end_to_end(_register_stub, monkeypatch, tmp_path):
    events, frames = _drive_voice_turn(monkeypatch, tmp_path, audio_out="wav")

    kinds = [name for name, _ in events]
    assert "user_transcript" in kinds
    assert "audio_start" in kinds
    assert "audio_end" in kinds
    assert kinds[-1] == "turn_done"
    assert frames and frames[0][:4] == b"RIFF"


def test_livehost_wav_downlink_pushes_binary_frame(_register_stub, monkeypatch, tmp_path):
    events, frames = _drive_voice_turn(monkeypatch, tmp_path, audio_out="wav")
    starts = [p for n, p in events if n == "audio_start"]
    assert starts and starts[0]["codec"] == "wav"
    assert frames and frames[0][:4] == b"RIFF"


def test_livehost_session_started_send_failure_does_not_leak_registry(_register_stub, monkeypatch, tmp_path):
    # Regression test: if the very first websocket.send_json (the "session_started"
    # event) raises -- e.g. because the client already disconnected -- the finally
    # block must still run ingestor.stop()/livehost_registry.unregister() cleanly,
    # without an UnboundLocalError on `current_turn` masking the original exception
    # and skipping cleanup.
    from starlette.websockets import WebSocket

    from app.services.livehost.registry import livehost_registry

    _register_stub.upsert(Profile(name="p2", stt=SttConfig(engine="stub-livehost")))
    _set_default_tts(monkeypatch, tmp_path, "stub-livehost-tts")

    original_send_json = WebSocket.send_json

    async def flaky_send_json(self, data, mode="text"):
        if isinstance(data, dict) and data.get("event") == "session_started":
            raise RuntimeError("client disconnected before session_started could be sent")
        return await original_send_json(self, data, mode=mode)

    monkeypatch.setattr(WebSocket, "send_json", flaky_send_json)

    client = TestClient(app)
    session_id = "test-session-started-send-failure"
    url = f"/v1/livehost/stream?profile=p2&sample_rate=16000&session_id={session_id}"
    try:
        with client.websocket_connect(url) as ws:
            # Block until the server-side task actually reaches the send_json
            # call and raises. Exiting the `with` block immediately would just
            # send a disconnect before the server got that far, never
            # exercising the fault we're injecting.
            ws.receive()
    except Exception:
        # The server-side handler raises (in the buggy version, an
        # UnboundLocalError masking the original RuntimeError); whether/how the
        # test transport surfaces that to the client isn't the point of this
        # test -- what matters is the registry cleanup below.
        pass

    assert livehost_registry.get(session_id) is None  # no leak from the masked-exception regression


def test_livehost_stream_passes_resolved_model_to_stt(_register_stub, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.api.routes.livehost.resolve_default_stt_model",
        lambda engine: "sentinel-model",
    )

    seen: list = []

    class _RecordingStub(STTProvider):
        name = "stub-livehost-record"

        async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
            seen.append(model)
            return STTResult(engine=self.name, text="ok", is_final=True)

    stt_service.providers["stub-livehost-record"] = _RecordingStub()
    try:
        _register_stub.upsert(Profile(name="p3", stt=SttConfig(engine="stub-livehost-record")))
        _set_default_tts(monkeypatch, tmp_path, "stub-livehost-tts")
        client = TestClient(app)
        url = "/v1/livehost/stream?profile=p3&sample_rate=16000"
        with client.websocket_connect(url) as ws:
            started = ws.receive_json()
            assert started["event"] == "session_started"

            ws.send_bytes(_loud(500))
            ws.send_bytes(_silence(500))
            ws.send_bytes(_silence(500))

            seen_events = 0
            while seen_events < 20:
                msg = ws.receive()
                if msg.get("bytes") is not None:
                    continue
                ev = json.loads(msg["text"])
                seen_events += 1
                if ev["event"] == "turn_done":
                    break
    finally:
        stt_service.providers.pop("stub-livehost-record", None)

    assert seen == ["sentinel-model"]


def test_livehost_voice_turn_stt_failure_still_sends_turn_done(_register_stub, monkeypatch, tmp_path):
    _register_stub.upsert(Profile(name="p4", stt=SttConfig(engine="stub-livehost-failing")))
    _set_default_tts(monkeypatch, tmp_path, "stub-livehost-tts")
    client = TestClient(app)
    url = "/v1/livehost/stream?profile=p4&sample_rate=16000"
    with client.websocket_connect(url) as ws:
        started = ws.receive_json()
        assert started["event"] == "session_started"

        ws.send_bytes(_loud(500))
        ws.send_bytes(_silence(500))
        ws.send_bytes(_silence(500))

        events = []
        for _ in range(20):
            ev = ws.receive_json()
            events.append(ev)
            if ev["event"] == "turn_done":
                break

        kinds = [e["event"] for e in events]
        assert "error" in kinds
        assert "turn_done" in kinds
        # the client must not be left hanging: error must arrive before turn_done
        assert kinds.index("error") < kinds.index("turn_done")
