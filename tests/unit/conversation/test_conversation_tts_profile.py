import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.core.audio import pcm16_to_wav_bytes
from app.main import app
from app.schemas.stt import STTResult
from app.services.profiles.models import Profile, SttConfig, TtsConfig
from app.services.profiles.store import ProfileStore
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.profile_models import TtsProfile
from app.services.tts.profile_store import TtsProfileStore
from app.services.tts.service import tts_service

SR = 16000


def _silence_wav(ms: int = 100, sr: int = 24000) -> bytes:
    n = int(sr * ms / 1000)
    return pcm16_to_wav_bytes(b"\x00\x00" * n, sample_rate=sr)


class _StubSTT(STTProvider):
    name = "stub-conv-ttsp"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chao", is_final=True)


class _RecordingTTS(TTSProvider):
    name = "stub-conv-ttsp-tts"

    def __init__(self) -> None:
        self.calls: list = []

    async def render_audio(self, payload) -> tuple[bytes, str]:
        self.calls.append(payload)
        return _silence_wav(), "audio/wav"


@pytest.fixture(autouse=True)
def _local_hermetic(monkeypatch, tmp_path):
    # Named distinctly from conftest.py's `_hermetic` so both autouse fixtures
    # run (a same-named fixture here would shadow, not compose with, the
    # global one).
    stt_service.providers["stub-conv-ttsp"] = _StubSTT()
    stub_tts = _RecordingTTS()
    tts_service.providers["stub-conv-ttsp-tts"] = stub_tts

    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    fresh_tts_profiles = TtsProfileStore(str(tmp_path / "tts_profiles.json"))
    monkeypatch.setattr("app.api.routes.conversation.profile_store", fresh_profiles)
    monkeypatch.setattr("app.api.routes.conversation.tts_profile_store", fresh_tts_profiles)

    from app.services import system_config as sc_mod

    fresh_config = sc_mod.SystemConfigStore(str(tmp_path / "system_config.json"))
    fresh_config.set(
        fresh_config.get().model_copy(
            update={
                "engines": fresh_config.get().engines.model_copy(
                    update={"default_stt_engine": "stub-conv-ttsp", "default_tts_engine": "stub-conv-ttsp-tts"}
                ),
            }
        )
    )
    monkeypatch.setattr("app.api.routes.conversation.system_config_store", fresh_config)
    monkeypatch.setattr(sc_mod, "system_config_store", fresh_config)

    yield stub_tts, fresh_profiles, fresh_tts_profiles

    stt_service.providers.pop("stub-conv-ttsp", None)
    tts_service.providers.pop("stub-conv-ttsp-tts", None)


@pytest.fixture
def client():
    return TestClient(app)


def _loud(ms: int) -> bytes:
    n = int(SR * ms / 1000)
    return (np.full(n, 0.2, dtype=np.float32) * 32767).astype("<i2").tobytes()


def _silence(ms: int) -> bytes:
    return (b"\x00\x00") * int(SR * ms / 1000)


def _run_one_turn(ws):
    ws.send_bytes(_loud(500))
    ws.send_bytes(_silence(500))
    ws.send_bytes(_silence(500))
    # 30 bounds JSON events only -- a skipped binary WAV frame (audio_out
    # defaults to wav now) must not burn out of the same budget as the
    # JSON events this loop is actually waiting on.
    json_count = 0
    while json_count < 30:
        msg = ws.receive()
        if msg.get("bytes") is not None:
            continue  # reply audio binary frame
        json_count += 1
        ev = json.loads(msg["text"])
        if ev["event"] == "turn_done":
            return


def test_tts_profile_linked_from_llm_profile_resolves_clone_fields(client, _local_hermetic):
    stub_tts, profiles, tts_profiles = _local_hermetic
    tts_profiles.upsert(TtsProfile(
        name="cloned-host", engine="stub-conv-ttsp-tts", voice_mode="clone",
        ref_audio_path="artifacts/refs/host.wav", ref_text="hello there",
        instruct="cheerful", speed=1.2, language="vi",
    ))
    profiles.upsert(Profile(name="host", tts=TtsConfig(profile_name="cloned-host")))

    url = "/v1/conversation/stream?profile=host&sample_rate=16000"
    with client.websocket_connect(url) as ws:
        assert ws.receive_json()["event"] == "session_started"
        _run_one_turn(ws)

    assert stub_tts.calls, "TTS provider was never invoked"
    payload = stub_tts.calls[0]
    assert payload.ref_audio_path == "artifacts/refs/host.wav"
    assert payload.ref_text == "hello there"
    assert payload.instruct == "cheerful"
    assert payload.speed == 1.2
    assert payload.language == "vi"


def test_query_param_tts_profile_overrides_llm_profile(client, _local_hermetic):
    stub_tts, profiles, tts_profiles = _local_hermetic
    tts_profiles.upsert(TtsProfile(name="from-llm-profile", engine="stub-conv-ttsp-tts", voice="v1"))
    tts_profiles.upsert(TtsProfile(
        name="pinned", engine="stub-conv-ttsp-tts", voice_mode="clone",
        ref_audio_path="artifacts/refs/ref.wav", ref_text="pinned voice",
    ))
    profiles.upsert(Profile(name="host", tts=TtsConfig(profile_name="from-llm-profile")))

    url = (
        "/v1/conversation/stream?profile=host"
        "&tts_profile=pinned&sample_rate=16000"
    )
    with client.websocket_connect(url) as ws:
        assert ws.receive_json()["event"] == "session_started"
        _run_one_turn(ws)

    payload = stub_tts.calls[0]
    assert payload.ref_audio_path == "artifacts/refs/ref.wav"
    assert payload.ref_text == "pinned voice"


def test_profile_only_connection_resolves_stt_from_profile(client, _local_hermetic):
    # A device connecting with just ?profile=<name> (no stt_engine/tts query params)
    # must resolve STT + TTS entirely from the profile — the whole point of the
    # profile-driven device config.
    stub_tts, profiles, tts_profiles = _local_hermetic
    tts_profiles.upsert(TtsProfile(name="host-voice", engine="stub-conv-ttsp-tts", voice="v1"))
    profiles.upsert(Profile(
        name="device",
        stt=SttConfig(engine="stub-conv-ttsp", language="vi"),
        tts=TtsConfig(profile_name="host-voice"),
    ))

    url = "/v1/conversation/stream?profile=device&sample_rate=16000"
    with client.websocket_connect(url) as ws:
        ready = ws.receive_json()
        assert ready["event"] == "session_started"
        assert ready["stt_engine"] == "stub-conv-ttsp"
        assert ready["language"] == "vi"
        assert ready["tts_engine"] == "stub-conv-ttsp-tts"
        _run_one_turn(ws)

    assert stub_tts.calls, "TTS provider was never invoked"


def test_no_tts_profile_falls_back_to_default_tts_engine(client, _local_hermetic):
    stub_tts, _profiles, _tts_profiles = _local_hermetic
    url = "/v1/conversation/stream?voice=manual-voice&sample_rate=16000"
    with client.websocket_connect(url) as ws:
        assert ws.receive_json()["event"] == "session_started"
        _run_one_turn(ws)

    payload = stub_tts.calls[0]
    assert payload.engine == "stub-conv-ttsp-tts"
    assert payload.voice == "manual-voice"
    assert payload.ref_audio_path is None
    assert payload.instruct is None
    assert payload.speed is None


def test_bad_ref_audio_path_degrades_to_tts_error(client, _local_hermetic):
    """Regression for task-6-fixes-round-1 I2, moved here from livehost's
    independent _synth (formerly api/routes/livehost.py) in Task 11 -- a
    second, separately-guarded code path from session.py's conversation turn,
    now reached from conversation/stream directly (livehost.py's own copy of
    this guarantee is retired: both sockets drive the same
    turn_stream.py/synthesize_or_degrade machinery).

    TtsProfile.ref_audio_path is deliberately NOT validated by the model
    itself (round-2 fix: a field_validator here would also run at DB LOAD
    time via SqliteBackedStore._ensure(), and one bad/host-mismatched row
    would then break every other stored profile too -- see
    profile_models.py's module docstring and test_tts_profile_store.py).
    The containment check instead lives in the route
    (api/routes/tts_profiles.py's create/update, a plain 422) and, as the
    real security boundary, in TTSRequest.ref_audio_path (schemas/tts.py) at
    every synthesis-time read. So a plain TtsProfile(...) call with a bad
    path (as below) no longer raises by itself -- this simulates a value
    that reached the store some other way (bypassing the route, e.g. a
    legacy row) -- the point is that even then, the turn must degrade to
    tts_error with the LLM text preserved, not unwind with a bare `error`."""
    stub_tts, profiles, tts_profiles = _local_hermetic
    bad_profile = TtsProfile(
        name="bad-ref", engine="stub-conv-ttsp-tts", voice_mode="clone",
        ref_audio_path="/etc/passwd", ref_text="x",
    )
    tts_profiles.upsert(bad_profile)
    profiles.upsert(Profile(name="host", tts=TtsConfig(profile_name="bad-ref")))

    url = "/v1/conversation/stream?profile=host&sample_rate=16000"
    events: list = []
    with client.websocket_connect(url) as ws:
        assert ws.receive_json()["event"] == "session_started"
        ws.send_json({"type": "text", "text": "hello"})
        for _ in range(20):
            ev = ws.receive_json()
            events.append(ev)
            if ev["event"] == "turn_done":
                break

    names = [e["event"] for e in events]
    # The LLM's reply text must survive the TTS failure, not be swallowed --
    # same "don't lose the reply" contract session.py's degrade path already
    # has a test for (test_session_bad_ref_audio_path_degrades.py).
    texts = [e["text"] for e in events if e["event"] == "response_text"]
    assert texts, names
    assert all(t for t in texts), texts
    assert "tts_error" in names, names
    assert "error" not in names, names
    tts_error = next(e for e in events if e["event"] == "tts_error")
    assert "ref_audio_path" in tts_error["message"]
    assert "artifacts directory" in tts_error["message"]
    assert not stub_tts.calls, "provider must never be reached -- TTSRequest construction fails first"
