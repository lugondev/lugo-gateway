import numpy as np
import pytest

from app.services.conversation.endpointer import VadEndpointer
from app.services.conversation.responder import EchoResponder

SR = 16000


def _loud(ms: int, amp: float = 0.2) -> bytes:
    n = int(SR * ms / 1000)
    return (np.full(n, amp, dtype=np.float32) * 32767).astype("<i2").tobytes()


def _silence(ms: int) -> bytes:
    n = int(SR * ms / 1000)
    return (b"\x00\x00") * n


def test_endpoint_after_trailing_silence():
    ep = VadEndpointer(SR, silence_ms=700, min_speech_ms=300, rms_threshold=0.015)
    assert ep.accept(_loud(500))["event"] == "speech_start"
    assert ep.accept(_loud(500)) is None  # still speaking
    assert ep.accept(_silence(500)) is None  # 500ms < 700ms
    ev = ep.accept(_silence(500))  # 1000ms >= 700ms -> endpoint
    assert ev["event"] == "endpoint"
    assert len(ev["audio"]) > 0
    assert ev["speech_ms"] >= 300


def test_short_blip_does_not_endpoint():
    ep = VadEndpointer(SR, silence_ms=300, min_speech_ms=500, rms_threshold=0.015)
    ep.accept(_loud(200))  # below min_speech_ms
    assert ep.accept(_silence(400)) is None  # too short to count as a turn


def test_idle_silence_ignored():
    ep = VadEndpointer(SR, silence_ms=200, min_speech_ms=100, rms_threshold=0.015)
    assert ep.accept(_silence(1000)) is None
    assert ep.speaking is False


def test_flush_returns_buffered_utterance():
    ep = VadEndpointer(SR, silence_ms=5000, min_speech_ms=200, rms_threshold=0.015)
    ep.accept(_loud(400))
    audio = ep.flush()
    assert audio and len(audio) > 0


@pytest.mark.asyncio
async def test_echo_responder_uses_last_user_turn():
    reply = await EchoResponder().reply([{"role": "user", "content": "xin chào"}])
    assert "xin chào" in reply


@pytest.mark.asyncio
async def test_echo_responder_handles_empty():
    reply = await EchoResponder().reply([])
    assert isinstance(reply, str) and reply
