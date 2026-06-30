"""prefetch_synthesis: synthesize the next sentence(s) while the current is being sent.

Borrowed from xiaozhi-server's tts_text_queue -> tts_audio_queue split: a worker runs
ahead of the consumer (bounded by `lookahead`) so the next chunk's audio is usually
ready before the current finishes -> gapless playback.
"""

import asyncio

import pytest

from app.services.tts.streaming import prefetch_synthesis


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


async def test_propagates_producer_error():
    async def boom_gen():
        yield "a"
        raise RuntimeError("producer died")

    async def synth(s):
        return s

    with pytest.raises(RuntimeError, match="producer died"):
        async for _x in prefetch_synthesis(boom_gen(), synth, lookahead=2):
            pass
