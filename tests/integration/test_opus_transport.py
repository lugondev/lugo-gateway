"""Opus audio transport: encode -> server decode -> endpointer/STT.

Skipped when libopus/opuslib is unavailable (e.g. CI without the system lib).
"""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.stt import STTResult
from app.schemas.tts import TTSResult
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service

try:
    # opuslib raises a bare Exception (not ImportError) when libopus can't be
    # found, so importorskip isn't enough — guard the whole load.
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
def _stub(monkeypatch):
    # conversation_llm_base_url now lives on system_config_store (Task 3), not
    # Settings; the module-level conftest._hermetic fixture already zeroes it.
    stt_service.providers["stub-opus"] = _StubSTT()
    tts_service.providers["stub-opus-tts"] = _StubTTS()
    yield
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


def test_conversation_opus_transport_turn():
    client = TestClient(app)
    url = "/v1/conversation/stream?stt_engine=stub-opus&tts_engine=stub-opus-tts&audio_codec=opus&sample_rate=16000"
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
