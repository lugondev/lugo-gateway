import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.stt import STTResult
from app.schemas.tts import TTSResult
from app.services.profiles.models import Profile, TtsConfig
from app.services.profiles.store import ProfileStore
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.profile_models import TtsProfile
from app.services.tts.profile_store import TtsProfileStore
from app.services.tts.service import tts_service

SR = 16000


class _StubSTT(STTProvider):
    name = "stub-livehost-ttsp"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="chao ban", is_final=True)


class _RecordingTTS(TTSProvider):
    name = "stub-livehost-ttsp-tts"

    def __init__(self) -> None:
        self.calls: list = []

    async def synthesize(self, payload) -> TTSResult:
        self.calls.append(payload)
        return TTSResult(
            engine=self.name, sample_rate=24000, audio_url="/artifacts/x.wav",
            duration_seconds=0.1, text=payload.text,
        )


@pytest.fixture(autouse=True)
def _local_hermetic(monkeypatch, tmp_path):
    # Named distinctly from conftest.py's `_hermetic` so both autouse fixtures
    # run (a same-named fixture here would shadow, not compose with, the
    # global one).
    stt_service.providers["stub-livehost-ttsp"] = _StubSTT()
    stub_tts = _RecordingTTS()
    tts_service.providers["stub-livehost-ttsp-tts"] = stub_tts

    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    fresh_tts_profiles = TtsProfileStore(str(tmp_path / "tts_profiles.json"))
    monkeypatch.setattr("app.api.routes.livehost.profile_store", fresh_profiles)
    monkeypatch.setattr("app.api.routes.livehost.tts_profile_store", fresh_tts_profiles)

    from app.services import system_config as sc_mod

    fresh_config = sc_mod.SystemConfigStore(str(tmp_path / "system_config.json"))
    fresh_config.set(
        fresh_config.get().model_copy(
            update={
                "engines": fresh_config.get().engines.model_copy(
                    update={"default_stt_engine": "stub-livehost-ttsp", "default_tts_engine": "stub-livehost-ttsp-tts"}
                ),
            }
        )
    )
    monkeypatch.setattr("app.api.routes.livehost.system_config_store", fresh_config)
    monkeypatch.setattr(sc_mod, "system_config_store", fresh_config)

    yield stub_tts, fresh_profiles, fresh_tts_profiles

    stt_service.providers.pop("stub-livehost-ttsp", None)
    tts_service.providers.pop("stub-livehost-ttsp-tts", None)


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
    for _ in range(20):
        ev = ws.receive_json()
        if ev["event"] == "turn_done":
            return


def test_livehost_tts_profile_linked_from_llm_profile(client, _local_hermetic):
    stub_tts, profiles, tts_profiles = _local_hermetic
    tts_profiles.upsert(TtsProfile(
        name="cloned-host", engine="stub-livehost-ttsp-tts", voice_mode="clone",
        ref_audio_path="artifacts/refs/host.wav", ref_text="hello there",
        instruct="cheerful", speed=1.1, language="vi",
    ))
    profiles.upsert(Profile(name="host", tts=TtsConfig(profile_name="cloned-host")))

    url = "/v1/livehost/stream?profile=host&sample_rate=16000"
    with client.websocket_connect(url) as ws:
        assert ws.receive_json()["event"] == "session_started"
        _run_one_turn(ws)

    assert stub_tts.calls, "TTS provider was never invoked"
    payload = stub_tts.calls[0]
    assert payload.ref_audio_path == "artifacts/refs/host.wav"
    assert payload.ref_text == "hello there"
    assert payload.instruct == "cheerful"
    assert payload.speed == 1.1
    assert payload.language == "vi"


def test_livehost_query_param_tts_profile_overrides_llm_profile(client, _local_hermetic):
    stub_tts, profiles, tts_profiles = _local_hermetic
    tts_profiles.upsert(TtsProfile(name="from-llm-profile", engine="stub-livehost-ttsp-tts", voice="v1"))
    tts_profiles.upsert(TtsProfile(
        name="pinned", engine="stub-livehost-ttsp-tts", voice_mode="clone",
        ref_audio_path="artifacts/refs/ref.wav", ref_text="pinned voice",
    ))
    profiles.upsert(Profile(name="host", tts=TtsConfig(profile_name="from-llm-profile")))

    url = (
        "/v1/livehost/stream?profile=host"
        "&tts_profile=pinned&sample_rate=16000"
    )
    with client.websocket_connect(url) as ws:
        assert ws.receive_json()["event"] == "session_started"
        _run_one_turn(ws)

    payload = stub_tts.calls[0]
    assert payload.ref_audio_path == "artifacts/refs/ref.wav"
    assert payload.ref_text == "pinned voice"


def test_livehost_bad_ref_audio_path_degrades_to_tts_error(client, _local_hermetic):
    """Regression for task-6-fixes-round-1 I2 on livehost's independent
    _synth (api/routes/livehost.py) -- a second, separately-guarded code
    path from session.py's conversation turn. TtsProfile.ref_audio_path now
    rejects an out-of-bounds path at construction time, so a normal
    TtsProfile(...) call here would itself raise; model_construct bypasses
    that to simulate a value that reached the store some other way (a
    legacy row, e.g.) -- the point is that even then, the turn must degrade
    to tts_error and finish, not unwind with a bare `error`."""
    stub_tts, profiles, tts_profiles = _local_hermetic
    bad_profile = TtsProfile.model_construct(
        name="bad-ref", owner_id=None, engine="stub-livehost-ttsp-tts", model_id="",
        voice_mode="clone", voice="", ref_audio_path="/etc/passwd", ref_text="x",
        instruct="", speed=None, language=None,
    )
    tts_profiles.upsert(bad_profile)
    profiles.upsert(Profile(name="host", tts=TtsConfig(profile_name="bad-ref")))

    url = "/v1/livehost/stream?profile=host&sample_rate=16000"
    events: list = []
    with client.websocket_connect(url) as ws:
        assert ws.receive_json()["event"] == "session_started"
        ws.send_bytes(_loud(500))
        ws.send_bytes(_silence(500))
        ws.send_bytes(_silence(500))
        for _ in range(20):
            ev = ws.receive_json()
            events.append(ev)
            if ev["event"] == "turn_done":
                break

    names = [e["event"] for e in events]
    assert "tts_error" in names, names
    assert "error" not in names, names
    tts_error = next(e for e in events if e["event"] == "tts_error")
    assert "ref_audio_path" in tts_error["message"]
    assert "artifacts directory" in tts_error["message"]
    assert not stub_tts.calls, "provider must never be reached -- TTSRequest construction fails first"


def test_livehost_no_tts_profile_falls_back_to_default_tts_engine(client, _local_hermetic):
    stub_tts, _profiles, _tts_profiles = _local_hermetic
    url = "/v1/livehost/stream?voice=manual-voice&sample_rate=16000"
    with client.websocket_connect(url) as ws:
        assert ws.receive_json()["event"] == "session_started"
        _run_one_turn(ws)

    payload = stub_tts.calls[0]
    assert payload.engine == "stub-livehost-ttsp-tts"
    assert payload.voice == "manual-voice"
    assert payload.ref_audio_path is None
    assert payload.instruct is None
