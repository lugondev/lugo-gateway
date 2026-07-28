"""Opus audio transport: encode -> server decode -> endpointer/STT.

Skipped when libopus/opuslib is unavailable (e.g. CI without the system lib).
"""

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

try:
    # opuslib raises a bare Exception (not ImportError) when libopus can't be
    # found, so importorskip isn't enough — guard the whole load. Check
    # app.core.opus.opus_available() first (rather than a bare `import
    # opuslib`): opuslib locates libopus via ctypes.util.find_library, which
    # needs app.core.opus._ensure_libopus_findable()'s shim to succeed on this
    # host. A bare import only works if some other, already-collected module
    # ran the shim first -- making whether this module skips depend on
    # collection order (see that module's docstring for why the shim exists).
    from app.core.opus import opus_available

    if not opus_available():
        raise RuntimeError("opus_available() reported libopus unavailable")
    import opuslib

    _ENC = opuslib.Encoder(16000, 1, opuslib.APPLICATION_VOIP)
except Exception:  # noqa: BLE001 - opuslib/libopus unavailable on this host
    pytest.skip("opuslib/libopus not loadable", allow_module_level=True)

SR = 16000
FRAME = 960  # 60 ms @ 16 kHz


class _StubSTT(STTProvider):
    name = "stub-opus"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chào", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-opus-tts"

    async def synthesize(self, payload) -> TTSResult:
        return TTSResult(
            engine=self.name, sample_rate=24000, audio_url="/artifacts/x.wav",
            duration_seconds=0.1, text=payload.text,
        )


@pytest.fixture(autouse=True)
def _stub(monkeypatch, tmp_path):
    # conversation_llm_base_url now lives on system_config_store; the
    # module-level conftest._hermetic fixture already zeroes it.
    stt_service.providers["stub-opus"] = _StubSTT()
    tts_service.providers["stub-opus-tts"] = _StubTTS()

    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    monkeypatch.setattr("app.api.routes.conversation.profile_store", fresh_profiles)

    from app.services import system_config as sc_mod

    fresh_config = sc_mod.SystemConfigStore(str(tmp_path / "system_config.json"))
    fresh_config.set(
        fresh_config.get().model_copy(
            update={"engines": fresh_config.get().engines.model_copy(update={"default_tts_engine": "stub-opus-tts"})}
        )
    )
    monkeypatch.setattr("app.api.routes.conversation.system_config_store", fresh_config)
    monkeypatch.setattr(sc_mod, "system_config_store", fresh_config)

    async def _ok_health(stt_engine, stt_model, tts_engine, tts_model):
        return (
            EngineHealth(engine=stt_engine, status="ok"),
            EngineHealth(engine=tts_engine, status="ok"),
        )

    # "stub-opus"/"stub-opus-tts" aren't recognized by stt_service/
    # tts_service's real engine-listing logic, so the Task 7 health gate's
    # check_resolved_engines() would KeyError trying to look them up. Stub the
    # gate out -- this file tests Opus transport, not the gate.
    monkeypatch.setattr("app.api.routes.conversation.check_resolved_engines", _ok_health)

    yield fresh_profiles

    stt_service.providers.pop("stub-opus", None)
    tts_service.providers.pop("stub-opus-tts", None)


def _opus_frames(samples: np.ndarray) -> list[bytes]:
    pcm = (samples * 32767).astype("<i2")
    frames = []
    for i in range(0, len(pcm) - FRAME, FRAME):
        frames.append(_ENC.encode(pcm[i : i + FRAME].tobytes(), FRAME))
    return frames


def test_opus_roundtrip_decodes_to_pcm():
    from app.core.opus import OpusFrameDecoder

    dec = OpusFrameDecoder(16000, 1)
    tone = 0.2 * np.sin(2 * np.pi * 220 * np.arange(FRAME) / SR).astype(np.float32)
    packet = _ENC.encode((tone * 32767).astype("<i2").tobytes(), FRAME)
    pcm = dec.decode(packet)
    assert len(pcm) == FRAME * 2  # 16-bit mono, same sample count


def test_conversation_opus_transport_turn(_stub):
    _stub.upsert(Profile(name="p-opus", stt=SttConfig(engine="stub-opus")))
    client = TestClient(app)
    url = "/v1/conversation/stream?profile=p-opus&audio_codec=opus&sample_rate=16000"
    with client.websocket_connect(url) as ws:
        started = ws.receive_json()
        assert started["event"] == "session_started"
        assert started["audio_codec"] == "opus"

        # A tone (not a DC constant — Opus wouldn't preserve that) as "speech".
        tone = 0.3 * np.sin(2 * np.pi * 220 * np.arange(SR) / SR).astype(np.float32)
        loud = _opus_frames(tone)                                # ~1s speech
        silence = _opus_frames(np.zeros(SR, dtype=np.float32))   # ~1s silence -> endpoint
        for f in loud + silence:
            ws.send_bytes(f)

        events = []
        for _ in range(40):
            ev = ws.receive_json()
            events.append(ev["event"])
            if ev["event"] == "turn_done":
                break
        assert "speech_start" in events
        assert "user_transcript" in events
        assert "turn_done" in events
