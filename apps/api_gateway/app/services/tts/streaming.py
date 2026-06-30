"""Prefetched TTS synthesis pipeline for gapless streaming playback.

Borrowed from xiaozhi-server's split between a text queue and an audio queue: a
worker synthesizes sentences *ahead* of the consumer (bounded by ``lookahead``), so
the next chunk's audio is usually ready before the current one finishes playing.
Without this, synthesis and sending run strictly sequentially and the client hears a
gap between sentences equal to the next sentence's synth time.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

_SENTINEL = object()


async def prefetch_synthesis(
    sentences: AsyncIterator[str],
    synth: Callable[[str], Awaitable],
    lookahead: int = 2,
) -> AsyncIterator[tuple[int, str, object]]:
    """Yield ``(index, sentence, audio)`` in order, synthesizing up to ``lookahead``
    sentences ahead of the consumer.

    ``synth`` is an async callable ``sentence -> audio`` (the audio payload is opaque
    here — caller decides what to do with it). Order is preserved. Exceptions from
    either ``sentences`` (the producer) or ``synth`` propagate to the consumer. The
    background worker is always cancelled on exit (normal, error, or cancellation), so
    a barge-in that cancels the consumer never leaks a synth worker.
    """
    if lookahead < 1:
        lookahead = 1
    queue: asyncio.Queue = asyncio.Queue(maxsize=lookahead)

    async def _worker() -> None:
        index = 0
        try:
            async for sentence in sentences:
                audio = await synth(sentence)
                await queue.put((index, sentence, audio))
                index += 1
            await queue.put(_SENTINEL)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - relay to the consumer
            await queue.put(exc)

    worker = asyncio.create_task(_worker())
    try:
        while True:
            item = await queue.get()
            if item is _SENTINEL:
                break
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        worker.cancel()
        try:
            await worker
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
