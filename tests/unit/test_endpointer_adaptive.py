"""Adaptive trailing-silence endpointing.

A long, clearly-finished utterance shouldn't need as much trailing silence to be
declared over as a short one (which may just be a mid-thought pause). The
endpointer scales the required trailing silence down from ``silence_ms`` toward
``min_silence_ms`` as the utterance grows past ``adaptive_full_ms``.
"""

import numpy as np

from app.services.conversation.endpointer import VadEndpointer

SR = 16000


def _loud(ms: int, amp: float = 0.2) -> bytes:
    n = int(SR * ms / 1000)
    return (np.full(n, amp, dtype=np.float32) * 32767).astype("<i2").tobytes()


def _silence(ms: int) -> bytes:
    n = int(SR * ms / 1000)
    return (b"\x00\x00") * n


def test_long_utterance_endpoints_after_min_silence():
    ep = VadEndpointer(
        SR, silence_ms=700, min_speech_ms=100, min_silence_ms=400, adaptive_full_ms=3000
    )
    assert ep.accept(_loud(3000))["event"] == "speech_start"  # ratio saturates -> 400ms
    assert ep.accept(_silence(300)) is None  # 300 < 400
    assert ep.accept(_silence(100))["event"] == "endpoint"  # 400 >= 400


def test_short_utterance_keeps_long_silence_window():
    ep = VadEndpointer(
        SR, silence_ms=700, min_speech_ms=100, min_silence_ms=400, adaptive_full_ms=3000
    )
    assert ep.accept(_loud(200))["event"] == "speech_start"  # barely adapts (~680ms)
    assert ep.accept(_silence(500)) is None  # 500 well under the ~680ms window
    assert ep.accept(_silence(300))["event"] == "endpoint"


def test_non_adaptive_by_default_uses_full_silence():
    # No min_silence_ms -> behaves exactly like the fixed-window endpointer.
    ep = VadEndpointer(SR, silence_ms=500, min_speech_ms=100)
    assert ep.accept(_loud(5000))["event"] == "speech_start"
    assert ep.accept(_silence(400)) is None  # 400 < 500, no early cut despite long speech
    assert ep.accept(_silence(100))["event"] == "endpoint"  # 500


def _captured_ms(preroll_ms: int) -> float:
    """Total audio (ms) the endpointer hands to STT for one utterance, given a
    pre-roll of `preroll_ms`. Feeds a long lead-in of near-silence (fills the
    pre-roll buffer past any window) then a fixed loud utterance."""
    ep = VadEndpointer(SR, silence_ms=300, min_speech_ms=100, preroll_ms=preroll_ms)
    for _ in range(25):                             # 25 x 60ms = 1.5s of lead-in,
        ep.accept(_silence(60))                     # fed as real-sized frames so the
                                                    # rolling buffer trims to preroll_ms
    assert ep.accept(_loud(500))["event"] == "speech_start"
    end = ep.accept(_silence(400))                  # 400 >= 300 -> endpoint
    assert end["event"] == "endpoint"
    return len(end["audio"]) / 2 / SR * 1000        # int16 mono -> ms


def test_larger_preroll_retains_more_onset():
    # The onset a device loses is exactly what falls outside the pre-roll window,
    # so a bigger pre-roll must keep proportionally more lead-in audio.
    assert _captured_ms(600) - _captured_ms(300) >= 250
