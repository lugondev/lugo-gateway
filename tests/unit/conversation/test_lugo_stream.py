import json

import numpy as np
import pytest
from fastapi.testclient import TestClient
from app.core.audio import pcm16_to_wav_bytes
from app.core.opus import OpusFrameEncoder, opus_available
from app.main import app
from app.schemas.stt import STTResult
from app.services.profiles.models import Profile, SessionConfig
from app.services.profiles.store import ProfileStore
from app.services.conversation.lugo_frame import LUGO_FRAME_OPUS
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.system_config import system_config_store
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service

# Real Opus encoding for the speech_start/speech_end VAD test below: the lugo
# route hardcodes audio_codec="opus" (see _resolve()/lugo_stream() in
# app.api.routes.lugo), so session.feed_audio() expects genuine Opus packets,
# not raw PCM16. Build packets via app.core.opus.OpusFrameEncoder -- the same
# helper session.py itself uses for outbound audio -- rather than calling
# opuslib directly: on macOS, opuslib's own ctypes.util.find_library('opus')
# lookup misses Homebrew's /opt/homebrew/lib, so a bare `import opuslib; then
# opuslib.Encoder(...)` (as tests/integration/test_opus_transport.py does)
# fails to load libopus and that whole test module ends up skipped on this
# host. OpusFrameEncoder runs app.core.opus's ctypes shim first, so it finds
# the same libopus session.py already loads successfully in production.
_OPUS_SR = 16000  # 16 kHz mono, 60 ms frames -- matches the wakeup audio_params below.


def _loud_opus(ms: int) -> list[bytes]:
    # A tone, not a DC constant -- Opus is lossy and wouldn't preserve a flat
    # DC level the way it preserves a tone's energy (same choice as
    # test_opus_transport.py). Amplitude 0.3 -> RMS ~0.21, comfortably above
    # settings.conversation_rms_threshold (0.015 by default).
    n = int(_OPUS_SR * ms / 1000)
    tone = 0.3 * np.sin(2 * np.pi * 220 * np.arange(n) / _OPUS_SR).astype(np.float32)
    pcm16 = (tone * 32767).astype("<i2").tobytes()
    return OpusFrameEncoder(sample_rate=_OPUS_SR, channels=1, frame_ms=60).encode_pcm16(pcm16)


def _silence_opus(ms: int) -> list[bytes]:
    n = int(_OPUS_SR * ms / 1000)
    pcm16 = b"\x00\x00" * n
    return OpusFrameEncoder(sample_rate=_OPUS_SR, channels=1, frame_ms=60).encode_pcm16(pcm16)


class _StubSTT(STTProvider):
    name = "stub-lugo-stt"
    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chao", is_final=True)


class _StubTTS(TTSProvider):
    # Lugo forces want_audio=True, so the core actually decodes the rendered
    # WAV to PCM for Opus encoding (unlike the Task-3 core test's
    # want_audio=False stub, which never touches it). render_audio() is the
    # only seam the session core calls now -- a missing implementation would
    # raise ProviderError mid-turn and the turn would never reach
    # audio_start/audio_end, hanging the test's receive loop.
    name = "stub-lugo-tts"

    async def render_audio(self, payload) -> tuple[bytes, str]:
        wav = pcm16_to_wav_bytes(b"\x00\x00" * 2400, sample_rate=24000)  # 100ms silence
        return wav, "audio/wav"


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
                "default_stt_engine": "stub-lugo-stt",
                "default_tts_engine": "stub-lugo-tts",
            })
        })

    monkeypatch.setattr(system_config_store, "get", _get_with_stub_engines)
    stt_service.providers["stub-lugo-stt"] = _StubSTT()
    tts_service.providers["stub-lugo-tts"] = _StubTTS()
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    fresh.upsert(Profile(name="dev", session=SessionConfig(idle_timeout_s=0)))
    monkeypatch.setattr("app.api.routes.lugo.profile_store", fresh)
    yield
    stt_service.providers.pop("stub-lugo-stt", None)
    tts_service.providers.pop("stub-lugo-tts", None)


def test_wakeup_gets_welcome_with_idle_timeout():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "version": 1, "profile": "dev",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        msg = ws.receive_json()
        assert msg["type"] == "welcome"
        assert "session_id" in msg
        assert msg["idle_timeout_s"] == 0


def test_text_turn_yields_stt_and_tts():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": "dev",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        assert ws.receive_json()["type"] == "welcome"
        ws.send_json({"type": "text", "text": "hi"})
        types = []
        first_binary_frame = None
        # Real opus packets are pushed as binary frames interleaved with the
        # JSON wire events (tts start -> N binary packets -> tts stop); skip
        # them here (aside from capturing the first one) since this loop
        # otherwise only asserts on the JSON event sequence.
        for _ in range(20):
            message = ws.receive()
            if message.get("bytes") is not None:
                if first_binary_frame is None:
                    first_binary_frame = message["bytes"]
                continue
            m = json.loads(message["text"])
            types.append((m["type"], m.get("state")))
            if m["type"] == "tts" and m.get("state") == "stop":
                break
        assert ("stt", None) in types
        assert any(t == "tts" for t, _ in types)
        assert first_binary_frame is not None
        assert first_binary_frame[0] == LUGO_FRAME_OPUS


def test_binary_first_frame_errors_not_crashes():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_bytes(b"\x00\x00")
        msg = ws.receive_json()
        assert msg["type"] == "error"


def test_non_json_first_frame_errors():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_text("not json")
        msg = ws.receive_json()
        assert msg["type"] == "error"


def test_non_dict_json_first_frame_errors():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_text("42")
        msg = ws.receive_json()
        assert msg["type"] == "error"


def test_non_numeric_sample_rate_falls_back():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": "dev",
                      "audio_params": {"format": "opus", "sample_rate": "fast"}})
        msg = ws.receive_json()
        assert msg["type"] == "welcome"


def test_tts_start_and_stop_bracket_a_turn():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": "dev",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        assert ws.receive_json()["type"] == "welcome"
        ws.send_json({"type": "text", "text": "hi"})
        states = []
        for _ in range(30):
            message = ws.receive()
            if message.get("bytes") is not None:
                continue
            m = json.loads(message["text"])
            if m["type"] == "tts":
                states.append(m["state"])
                if m["state"] == "stop":
                    break
        assert states[0] == "start"
        assert states[-1] == "stop"
        assert states.count("start") == 1
        assert states.count("stop") == 1
        assert "sentence_start" in states


def test_tts_bracket_without_opus_encoder(monkeypatch):
    # lugo.py itself gates want_audio on opus_available() before the session is
    # even built (the device wire protocol has no non-Opus audio frame -- see
    # test_lugo_falls_back_to_text_only_without_libopus below), so this turn
    # runs text-only: the "tts" start/sentence_start/stop bracket in lugo.py's
    # emit() is driven by response_text/turn_done regardless of whether any
    # audio was ever produced, and must still bracket the turn correctly here.
    # session.py/lugo.py both import `opus_available` locally (`from
    # app.core.opus import ... opus_available`), not at module scope, so
    # patching the function on its defining module (app.core.opus) is what
    # actually takes effect at call time -- patching a same-named attribute on
    # the session/lugo module would be a no-op since neither binds it at
    # module scope.
    monkeypatch.setattr("app.core.opus.opus_available", lambda: False)
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": "dev",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        assert ws.receive_json()["type"] == "welcome"
        ws.send_json({"type": "text", "text": "hi"})
        states = []
        for _ in range(30):
            message = ws.receive()
            if message.get("bytes") is not None:
                continue
            m = json.loads(message["text"])
            if m["type"] == "tts":
                states.append(m["state"])
                if m["state"] == "stop":
                    break
        assert states[0] == "start"
        assert states[-1] == "stop"
        assert states.count("start") == 1
        assert states.count("stop") == 1
        assert "sentence_start" in states


def test_lugo_falls_back_to_text_only_without_libopus(monkeypatch):
    """Without libopus, the device wire protocol (lugo_frame.py) has no frame
    type for anything but Opus -- pushing raw WAV bytes through emit_audio
    would have them decoded as a corrupt Opus packet on the device. lugo.py
    must ask for a text-only session (want_audio=False) instead of letting
    session.start()'s opus->wav downgrade produce audio bytes at all: no
    binary frame may ever reach the client, while the turn's text (tts
    start/sentence_start/stop, driven by response_text/turn_done) still does."""
    monkeypatch.setattr("app.core.opus.opus_available", lambda: False)
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": "dev",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        assert ws.receive_json()["type"] == "welcome"
        ws.send_json({"type": "text", "text": "hi"})
        binary_frames = 0
        saw_tts_start = saw_tts_stop = False
        for _ in range(30):
            message = ws.receive()
            if message.get("bytes") is not None:
                binary_frames += 1
                continue
            m = json.loads(message["text"])
            if m["type"] == "tts" and m.get("state") == "start":
                saw_tts_start = True
            if m["type"] == "tts" and m.get("state") == "stop":
                saw_tts_stop = True
                break
        assert binary_frames == 0, "no libopus -- the device must receive no binary frame at all"
        assert saw_tts_start and saw_tts_stop


def test_welcome_honors_requested_output_sample_rate():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({
            "type": "wakeup",
            "profile": None,
            "audio_params": {"sample_rate": 16000, "output_sample_rate": 16000},
        })
        welcome = ws.receive_json()
        assert welcome["type"] == "welcome"
        assert welcome["audio_params"]["sample_rate"] == 16000


def test_wakeup_with_session_id_resumes_and_echoes_same_id():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": "dev", "session_id": "resume-me-123",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        msg = ws.receive_json()
        assert msg["type"] == "welcome"
        assert msg["session_id"] == "resume-me-123"


def test_wakeup_without_session_id_gets_a_fresh_uuid():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": "dev",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        msg = ws.receive_json()
        assert msg["type"] == "welcome"
        assert msg["session_id"] != "resume-me-123"
        assert len(msg["session_id"]) == 36  # uuid4 string form


def test_welcome_includes_engine_ready_flags():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": "dev",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        msg = ws.receive_json()
        assert msg["type"] == "welcome"
        assert msg["stt_ready"] is True
        assert msg["tts_ready"] is True


def test_engines_ready_is_forwarded_when_initially_cold(monkeypatch):
    monkeypatch.setattr("app.services.conversation.session.is_ready", lambda _provider: False)
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": "dev",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        welcome = ws.receive_json()
        assert welcome["stt_ready"] is False
        assert welcome["tts_ready"] is False
        msg = ws.receive_json()
        assert msg["type"] == "engines_ready"


def test_speech_and_processing_events_are_forwarded():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": "dev",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        assert ws.receive_json()["type"] == "welcome"
        ws.send_json({"type": "text", "text": "hi"})
        types_seen = []
        for _ in range(30):
            message = ws.receive()
            if message.get("bytes") is not None:
                continue
            m = json.loads(message["text"])
            types_seen.append(m["type"])
            if m["type"] == "tts" and m.get("state") == "stop":
                break
        # A text turn has no speech_start/speech_end (those are audio-VAD-driven),
        # but "processing" fires for every turn regardless of input modality.
        assert "processing" in types_seen


def test_speech_start_and_speech_end_are_forwarded_for_real_audio():
    # Unlike the text-turn test above, this drives real (Opus-encoded) audio
    # through the VAD endpointer so speech_start/speech_end actually fire from
    # ConversationSession.feed_audio(), not just "processing".
    if not opus_available():
        pytest.skip("opuslib/libopus not loadable on this host")
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": "dev",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        assert ws.receive_json()["type"] == "welcome"

        # ~1s of loud tone (>> conversation_min_speech_ms) then ~1s of silence
        # (>> conversation_silence_ms=700ms) to reliably cross both VAD
        # thresholds -- same durations already proven to trigger speech_start/
        # endpoint in tests/integration/test_opus_transport.py's real-Opus turn.
        for pkt in _loud_opus(1000):
            ws.send_bytes(pkt)
        for pkt in _silence_opus(1000):
            ws.send_bytes(pkt)

        types_seen = []
        speech_end_msg = None
        for _ in range(60):
            message = ws.receive()
            if message.get("bytes") is not None:
                continue
            m = json.loads(message["text"])
            if m["type"] == "engines_ready":
                continue
            types_seen.append(m["type"])
            if m["type"] == "speech_end":
                speech_end_msg = m
            if m["type"] == "tts" and m.get("state") == "stop":
                break

        assert "speech_start" in types_seen
        assert "speech_end" in types_seen
        assert types_seen.index("speech_start") < types_seen.index("speech_end")
        # speech_start/speech_end are VAD signals that must reach the device
        # before the turn's own stt/tts traffic starts.
        first_turn_msg = next(i for i, t in enumerate(types_seen) if t in ("stt", "tts"))
        assert types_seen.index("speech_end") < first_turn_msg
        assert speech_end_msg is not None
        assert "speech_ms" in speech_end_msg
        assert isinstance(speech_end_msg["speech_ms"], int)


def test_aborted_reason_is_included_on_tts_stop():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": "dev",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        assert ws.receive_json()["type"] == "welcome"
        ws.send_json({"type": "text", "text": "hi"})
        # Wait for the turn to actually start speaking, then abort mid-reply.
        for _ in range(30):
            message = ws.receive()
            if message.get("bytes") is not None:
                continue
            m = json.loads(message["text"])
            if m["type"] == "tts" and m.get("state") == "start":
                break
        ws.send_json({"type": "abort"})
        for _ in range(30):
            message = ws.receive()
            if message.get("bytes") is not None:
                continue
            m = json.loads(message["text"])
            if m["type"] == "tts" and m.get("state") == "stop":
                assert m.get("reason") == "barge-in"
                return
        raise AssertionError("never saw tts stop after abort")
