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


def pacing_delays(n_packets: int, prebuffer: int, frame_s: float) -> list[float]:
    """Delay (seconds) to wait BEFORE sending each Opus packet, for real-time pacing.

    The first ``prebuffer`` packets go out with no delay to fill the device's jitter
    buffer fast (lowest first-audio latency); every packet after that is paced by one
    frame duration so the server emits at playback rate and the device buffer stays
    bounded instead of being flooded. Mirrors xiaozhi-server's pre-buffer +
    AudioRateController.
    """
    return [0.0 if i < prebuffer else frame_s for i in range(n_packets)]


async def prefetch_synthesis(
    sentences: AsyncIterator[str],
    synth: Callable[[str], Awaitable],
    lookahead: int = 2,
) -> AsyncIterator[tuple[int, str, object]]:
    """Yield ``(index, sentence, audio)`` in order, synthesizing up to ``lookahead``
    sentences ahead of the consumer.

    Two independent background tasks, not one: a producer task drains
    ``sentences`` (the LLM stream) into a text queue, and a separate
    synthesizer task drains that queue into synth() calls. Splitting them
    means pulling sentence N+1 off the LLM stream can happen WHILE synth(N)
    is still running, instead of only being asked for after synth(N)
    completes -- a single combined worker would pay LLM-network-wait time
    and synth time back-to-back instead of overlapping them.

    ``synth`` is an async callable ``sentence -> audio`` (the audio payload is opaque
    here — caller decides what to do with it). Order is preserved. Exceptions from
    either ``sentences`` (the producer) or ``synth`` propagate to the consumer. Both
    background tasks are always cancelled on exit (normal, error, or cancellation), so
    a barge-in that cancels the consumer never leaks a producer or synth task.
    """
    if lookahead < 1:
        lookahead = 1
    text_queue: asyncio.Queue = asyncio.Queue(maxsize=lookahead)
    audio_queue: asyncio.Queue = asyncio.Queue(maxsize=lookahead)

    async def _produce() -> None:
        try:
            async for sentence in sentences:
                await text_queue.put(sentence)
            await text_queue.put(_SENTINEL)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - relay to the synthesizer
            await text_queue.put(exc)

    async def _synthesize() -> None:
        index = 0
        try:
            while True:
                item = await text_queue.get()
                if item is _SENTINEL:
                    break
                if isinstance(item, BaseException):
                    raise item
                audio = await synth(item)
                await audio_queue.put((index, item, audio))
                index += 1
            await audio_queue.put(_SENTINEL)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - relay to the consumer
            await audio_queue.put(exc)

    producer = asyncio.create_task(_produce())
    synthesizer = asyncio.create_task(_synthesize())
    try:
        while True:
            item = await audio_queue.get()
            if item is _SENTINEL:
                break
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        producer.cancel()
        synthesizer.cancel()
        for task in (producer, synthesizer):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
