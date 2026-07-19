"""prefetch_synthesis: synthesize the next sentence(s) while the current is being sent.

Borrowed from xiaozhi-server's tts_text_queue -> tts_audio_queue split: a worker runs
ahead of the consumer (bounded by `lookahead`) so the next chunk's audio is usually
ready before the current finishes -> gapless playback.
"""

import asyncio

import pytest

from app.services.tts.streaming import pacing_delays, prefetch_synthesis


async def _agen(items):
    for it in items:
        yield it


async def test_preserves_order_and_indexes():
    async def synth(s):
        return s.upper()

    got = [x async for x in prefetch_synthesis(_agen(["a", "b", "c"]), synth, lookahead=2)]
    assert got == [(0, "a", "A"), (1, "b", "B"), (2, "c", "C")]


async def test_synthesizes_ahead_while_consuming_current():
    started: list[str] = []

    async def synth(s):
        started.append(s)
        return s.upper()

    agen = prefetch_synthesis(_agen(["a", "b", "c"]), synth, lookahead=2)
    first = await agen.__anext__()
    await asyncio.sleep(0.01)  # let the worker run ahead
    assert first == (0, "a", "A")
    # the worker prefetched the next sentence(s) while we held only the first
    assert "b" in started, "synthesis did not run ahead of consumption"
    rest = [x async for x in agen]
    assert rest == [(1, "b", "B"), (2, "c", "C")]


async def test_lookahead_bounds_runahead():
    # With lookahead=1 the worker may be at most 1 sentence ahead of the consumer.
    started: list[str] = []
    consumed: list[str] = []

    async def synth(s):
        started.append(s)
        await asyncio.sleep(0.005)
        return s

    async for _idx, s, _audio in prefetch_synthesis(_agen(list("abcde")), synth, lookahead=1):
        consumed.append(s)
        # worker is at most lookahead(1) + 1 (the in-flight one) ahead of what we consumed
        assert len(started) - len(consumed) <= 2


async def test_propagates_synth_error():
    async def synth(s):
        if s == "b":
            raise ValueError("boom")
        return s

    agen = prefetch_synthesis(_agen(["a", "b", "c"]), synth, lookahead=2)
    got = []
    with pytest.raises(ValueError, match="boom"):
        async for x in agen:
            got.append(x)
    assert got[0] == (0, "a", "a")


def test_pacing_delays_prebuffer_then_paced():
    # First `prebuffer` frames go out immediately (fill the device jitter buffer),
    # then each subsequent frame is paced by one frame duration (real-time playback).
    d = pacing_delays(10, prebuffer=3, frame_s=0.06)
    assert d[:3] == [0.0, 0.0, 0.0]
    assert d[3:] == [0.06] * 7
    assert len(d) == 10


def test_pacing_delays_all_immediate_when_fewer_than_prebuffer():
    assert pacing_delays(2, prebuffer=5, frame_s=0.06) == [0.0, 0.0]


def test_pacing_delays_empty():
    assert pacing_delays(0, prebuffer=5, frame_s=0.06) == []


async def test_pulls_next_sentence_from_producer_while_synth_is_in_flight():
    """The old single-worker design could only ask the sentence producer (the
    LLM stream) for the next sentence AFTER synth() of the current one
    finished -- so if the LLM had already streamed sentence N+1's text, that
    text still sat unused until synth(N) completed, adding LLM-network-wait
    time on top of synth time instead of overlapping with it. The producer
    and the synthesizer must run as independent tasks so pulling sentence N+1
    from the LLM stream can happen WHILE synth(N) is still running."""
    pulled: list[str] = []
    synth_started = asyncio.Event()
    let_synth_finish = asyncio.Event()

    async def agen():
        for it in ["a", "b"]:
            pulled.append(it)
            yield it

    async def synth(s):
        if s == "a":
            synth_started.set()
            await let_synth_finish.wait()  # hold synth("a") open
        return s.upper()

    gen = prefetch_synthesis(agen(), synth, lookahead=2)
    task = asyncio.ensure_future(gen.__anext__())
    await synth_started.wait()
    await asyncio.sleep(0.01)  # give the producer task a chance to run
    assert pulled == ["a", "b"], "producer should pull 'b' while synth('a') is still in flight"

    let_synth_finish.set()
    result = await task
    assert result == (0, "a", "A")
    rest = [x async for x in gen]
    assert rest == [(1, "b", "B")]


async def test_propagates_producer_error():
    async def boom_gen():
        yield "a"
        raise RuntimeError("producer died")

    async def synth(s):
        return s

    with pytest.raises(RuntimeError, match="producer died"):
        async for _x in prefetch_synthesis(boom_gen(), synth, lookahead=2):
            pass
