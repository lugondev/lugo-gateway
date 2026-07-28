"""Per-connection opus_pace override: proves the web client can disable
server-side Opus playback pacing for its own session without touching the
global config that api/routes/lugo.py (ESP32/RPi) relies on. See
docs/superpowers/specs/2026-07-28-web-audio-jitter-buffer-design.md."""

import json
import time

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
from app.services.tts.profile_models import TtsProfile
from app.services.tts.profile_store import TtsProfileStore
from app.services.tts.service import tts_service


def _opus_ok():
    # Same pattern as tests/integration/test_gateway_modalities.py's
    # _opus_ok(): must route through opus_available()'s libopus-findable shim,
    # a bare `import opuslib` depends on collection order.
    from app.core.opus import opus_available

    if not opus_available():
        return False
    import opuslib

    try:
        opuslib.Encoder(24000, 1, opuslib.APPLICATION_VOIP)
        return True
    except Exception:  # noqa: BLE001
        return False


class _StubSTT(STTProvider):
    name = "stub-pace-stt"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chao", is_final=True)


class _StubTTS(RenderingTTSProvider):
    """600ms of silence -> 10 Opus frames (60ms each) per sentence synthesized.
    The profile has no LLM configured, so the turn runs through the built-in
    EchoResponder, which replies with 3 sentences (see
    app/services/conversation/responder.py's EchoResponder.reply) -- each
    gets synthesized separately, so a full turn emits 3 * 10 = 30 frames,
    all paced on one global clock (see _stream_to_tts's "Global real-time
    pacer for the WHOLE reply" comment). 30 frames is well past the default
    5-frame prebuffer, so paced vs unpaced delivery time is measurably
    different (see the two tests below)."""

    name = "stub-pace-tts"
    sample_rate = 24000

    async def _render_wav(self, payload: TTSRequest) -> bytes:
        n = int(self.sample_rate * 600 / 1000)
        return pcm16_to_wav_bytes(b"\x00\x00" * n, sample_rate=self.sample_rate)


@pytest.fixture(autouse=True)
def _stub(monkeypatch, tmp_path):
    stt_service.providers["stub-pace-stt"] = _StubSTT()
    tts_service.providers["stub-pace-tts"] = _StubTTS()

    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    fresh_profiles.upsert(Profile(name="p-pace", stt=SttConfig(engine="stub-pace-stt")))
    monkeypatch.setattr("app.api.routes.conversation.profile_store", fresh_profiles)

    fresh_tts_profiles = TtsProfileStore(str(tmp_path / "tts_profiles.json"))
    fresh_tts_profiles.upsert(TtsProfile(name="p-pace-tts", engine="stub-pace-tts"))
    monkeypatch.setattr("app.api.routes.conversation.tts_profile_store", fresh_tts_profiles)

    # system_config_store is a real singleton shared by every test in the run
    # (see conftest.py's _hermetic docstring) -- mutating it in place would
    # leak into unrelated tests, so point at a fresh, tmp_path-scoped store
    # instead. We don't change any values on it (conversation_opus_pace's
    # real default is True, conversation_opus_prebuffer_frames's is 5 --
    # exactly what these tests want to prove behavior against); this is only
    # isolation from whatever state the shared store happens to be in.
    #
    # All THREE modules that did `from app.services.system_config import
    # system_config_store` hold independent name bindings and must each be
    # patched individually -- patching only `app.api.routes.conversation` and
    # `app.services.system_config` (the "dual-binding gotcha" pattern used
    # elsewhere in this suite, e.g. test_gateway_modalities.py) does NOT reach
    # `app.services.conversation.session`, which is where the actual pacing
    # decision is made. Verified empirically: after that two-module patch,
    # `app.services.conversation.session.system_config_store is fresh_config`
    # is False.
    from app.services import system_config as sc_mod

    fresh_config = sc_mod.SystemConfigStore(str(tmp_path / "system_config.json"))
    monkeypatch.setattr("app.api.routes.conversation.system_config_store", fresh_config)
    monkeypatch.setattr("app.services.conversation.session.system_config_store", fresh_config)
    monkeypatch.setattr(sc_mod, "system_config_store", fresh_config)

    yield

    stt_service.providers.pop("stub-pace-stt", None)
    tts_service.providers.pop("stub-pace-tts", None)


def _drain_frames(ws, max_events=100):
    """Consume one whole turn; return (binary_frame_count, elapsed_seconds)."""
    t0 = time.monotonic()
    frames = 0
    for _ in range(max_events):
        msg = ws.receive()
        if msg.get("bytes") is not None:
            frames += 1
            continue
        ev = json.loads(msg["text"])["event"]
        if ev == "turn_done":
            break
    return frames, time.monotonic() - t0


@pytest.mark.skipif(not _opus_ok(), reason="libopus not loadable")
def test_opus_pace_query_override_skips_realtime_pacing():
    # Global conversation_opus_pace stays at its real default (True) -- the
    # query param must override it for THIS session only.
    c = TestClient(app)
    url = (
        "/v1/conversation/stream?profile=p-pace&tts_profile=p-pace-tts"
        "&output=audio&audio_out=opus&output_sample_rate=24000&opus_pace=0"
    )
    with c.websocket_connect(url) as ws:
        assert ws.receive_json()["event"] == "session_started"
        ws.send_json({"type": "text", "text": "xin chao"})
        frames, elapsed = _drain_frames(ws)
    assert frames == 30
    # True paced delivery of 30 frames past a 5-frame prebuffer takes >=1.5s
    # (see the next test) -- unpaced must be far under that.
    assert elapsed < 0.15


@pytest.mark.skipif(not _opus_ok(), reason="libopus not loadable")
def test_opus_pace_omitted_still_paces_by_default():
    # No opus_pace param -- must inherit the global default (True), exactly
    # what api/routes/lugo.py (ESP32/RPi) gets today. This test passes
    # unchanged before AND after Task 1's production code -- it's a
    # regression guard, not new behavior.
    c = TestClient(app)
    url = (
        "/v1/conversation/stream?profile=p-pace&tts_profile=p-pace-tts"
        "&output=audio&audio_out=opus&output_sample_rate=24000"
    )
    with c.websocket_connect(url) as ws:
        assert ws.receive_json()["event"] == "session_started"
        ws.send_json({"type": "text", "text": "xin chao"})
        frames, elapsed = _drain_frames(ws)
    assert frames == 30
    # 30 frames, 5-frame prebuffer -> 25 frames paced 60ms apart = >=1.5s.
    assert elapsed >= 1.4
