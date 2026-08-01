"""A noise burst too short to be an utterance must not wedge the endpointer.

Regression test for an unbounded buffer: `_speaking` was set by any speech
frame, but the endpoint condition also required `speech_ms >= min_speech_ms`.
A burst below that minimum (a cough, a door, a click) therefore left the
endpointer permanently "speaking" -- the condition could never become true, so
every subsequent SILENCE frame kept appending to `_collected`, forever.

Two consequences, both observed on an always-on device:
  * ~32KB/s of growth on a 16kHz link with no upper bound (`max_utterance_ms`
    caps `speech_ms`, not the buffer), i.e. ~115MB/hour per connection;
  * when the user finally did speak, that entire silent blob was prepended to
    the utterance handed to STT.
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


def test_short_burst_then_silence_releases_the_buffer():
    ep = VadEndpointer(SR, silence_ms=700, min_speech_ms=300)
    assert ep.accept(_loud(200))["event"] == "speech_start"  # 200ms < 300ms minimum
    assert ep.accept(_silence(800)) is None  # past the window, but too short to emit

    assert ep.speaking is False
    assert len(ep._collected) == 0


def test_short_burst_does_not_grow_the_buffer_without_bound():
    ep = VadEndpointer(SR, silence_ms=700, min_speech_ms=300)
    ep.accept(_loud(200))
    for _ in range(300):  # 60s of silence, 200ms at a time
        assert ep.accept(_silence(200)) is None
    # Bounded by the pre-roll window, not by how long the room stayed quiet.
    assert len(ep._collected) == 0
    assert ep._preroll_ms <= ep.preroll_ms + 200


def test_speech_after_a_short_burst_is_clean():
    """The utterance handed to STT must not carry the earlier silence."""
    ep = VadEndpointer(SR, silence_ms=700, min_speech_ms=300, preroll_ms=0)
    ep.accept(_loud(200))
    for _ in range(50):  # 10s of quiet
        ep.accept(_silence(200))

    assert ep.accept(_loud(1000))["event"] == "speech_start"
    event = ep.accept(_silence(800))
    assert event["event"] == "endpoint"
    # 1000ms of speech + 800ms of trailing silence + at most one retained
    # pre-roll frame (the deque never drops its last entry) -- NOT the 10s of
    # silence that preceded it.
    seconds = len(event["audio"]) / 2 / SR
    assert 1.8 <= seconds <= 2.1


def test_a_real_utterance_still_endpoints():
    ep = VadEndpointer(SR, silence_ms=700, min_speech_ms=300)
    assert ep.accept(_loud(500))["event"] == "speech_start"
    assert ep.accept(_silence(800))["event"] == "endpoint"
