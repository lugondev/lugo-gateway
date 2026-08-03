"""The reply pacer must never hand the device more than a prebuffer of frames.

turn_stream.py's module docstring promises: "A single clock keeps the device
buffer ~prebuffer-deep for the entire reply." That promise is what stops the
gateway overrunning the ESP32's downlink queue -- ``DL_QUEUE_DEPTH`` is 32
frames and ``dl_push`` is ``xQueueSend(..., 0)``, which DROPS silently when
full (main.c's ``s_dl_drops``). Dropped Opus frames leave the device decoder
resuming from stale state, which is audible as a warbly first word.

The promise only held while synthesis was faster than playback. Every TTS
engine we ship for Vietnamese runs at RTF > 1 (measured against the live
vieneu container: 1.11-1.34), so the release clock falls behind by
``synth - audio`` seconds on every sentence. Arrears that big used to be
settled all at once -- the whole next sentence went out with no sleep at all.

These tests pin the invariant directly on the clock, with an injected `now`,
so they say what the arithmetic must do without depending on real timing.
"""

from app.services.conversation.turn_stream import ReplyPacer

FRAME_S = 0.060
PREBUF = 5


def _burst(pacer, now, limit=200):
    """How many frames the pacer releases without ever asking to sleep."""
    n = 0
    while n < limit and pacer.delay_before_next(now) <= 0:
        n += 1
    return n


def test_prebuffer_frames_go_out_immediately():
    # The prebuffer is the whole point: the device holds playback until it has
    # SPK_PREBUFFER_FRAMES buffered, so the head of a reply must not be paced.
    # PREBUF + 1 frames, not PREBUF: the frame after the prebuffer is due at
    # t0 exactly (playback has consumed nothing yet), so it is owed now too.
    # That +1 is the bound every burst assertion below uses.
    pacer = ReplyPacer(prebuffer=PREBUF, frame_s=FRAME_S)
    assert _burst(pacer, now=100.0) == PREBUF + 1


def test_steady_state_paces_one_frame_per_frame_duration():
    pacer = ReplyPacer(prebuffer=PREBUF, frame_s=FRAME_S)
    now = 100.0
    for _ in range(PREBUF):
        assert pacer.delay_before_next(now) <= 0
    # Past the prebuffer the clock takes over: one frame per frame duration.
    assert pacer.delay_before_next(now) == 0.0          # frame PREBUF is due now
    assert abs(pacer.delay_before_next(now) - FRAME_S) < 1e-9
    assert abs(pacer.delay_before_next(now) - 2 * FRAME_S) < 1e-9


def test_slow_synthesis_does_not_release_an_unbounded_burst():
    """The bug: an engine slower than real time makes the clock owe arrears,
    and the arrears used to be paid off in one burst far bigger than the
    device's 32-frame queue."""
    pacer = ReplyPacer(prebuffer=PREBUF, frame_s=FRAME_S)
    now = 100.0
    # Sentence 0: 36 frames, released over its own 2.16s of playback.
    for _ in range(36):
        now += max(pacer.delay_before_next(now), 0.0)
    # Sentence 1 took 5.38s to synthesize while only 2.16s played -- the
    # measured vieneu case. The clock is now ~3.2s (53 frames) in arrears.
    now += 5.38 - 2.16
    released = _burst(pacer, now)
    assert released <= PREBUF + 1, (
        f"pacer released {released} frames back-to-back after a slow sentence; "
        f"the device queue holds 32 and drops the rest"
    )


def test_arrears_do_not_accumulate_across_sentences():
    """Three slow sentences in a row must not compound into a bigger burst --
    that is what made late sentences in a long reply the worst."""
    pacer = ReplyPacer(prebuffer=PREBUF, frame_s=FRAME_S)
    now = 100.0
    worst = 0
    for _ in range(3):
        for _ in range(36):
            now += max(pacer.delay_before_next(now), 0.0)
        now += 3.2   # synth overran this sentence's playback by 3.2s
        worst = max(worst, _burst(pacer, now))
    assert worst <= PREBUF + 1


def test_a_fast_engine_is_still_paced_to_real_time():
    """Regression guard for the other direction: when synthesis is FASTER than
    playback the pacer must still hold frames back, or the device floods."""
    pacer = ReplyPacer(prebuffer=PREBUF, frame_s=FRAME_S)
    now = 100.0
    for _ in range(PREBUF):
        pacer.delay_before_next(now)
    # 50 frames = 3s of audio, all ready instantly.
    total_sleep = 0.0
    for _ in range(50):
        total_sleep += max(pacer.delay_before_next(now + total_sleep), 0.0)
    assert abs(total_sleep - 49 * FRAME_S) < 1e-6
