import asyncio

import pytest

from app.schemas.common import StreamEvent
from app.streaming.event_bus import InMemoryEventBus


def _event(event_type: str, seq: int) -> StreamEvent:
    return StreamEvent(event_type=event_type, job_id="j1", sequence=seq)


async def test_live_subscriber_receives_events():
    bus = InMemoryEventBus()
    queue = bus.subscribe("job:1")
    await bus.publish("job:1", _event("audio_chunk", 1))
    received = await asyncio.wait_for(queue.get(), timeout=1)
    assert received.event_type == "audio_chunk"


async def test_late_subscriber_gets_replay():
    """A subscriber that arrives after publishing still receives buffered events."""
    bus = InMemoryEventBus()
    await bus.publish("job:1", _event("queued", 1))
    await bus.publish("job:1", _event("audio_chunk", 2))

    queue = bus.subscribe("job:1")
    first = await asyncio.wait_for(queue.get(), timeout=1)
    second = await asyncio.wait_for(queue.get(), timeout=1)
    assert first.event_type == "queued"
    assert second.event_type == "audio_chunk"


async def test_done_closes_channel_with_sentinel():
    bus = InMemoryEventBus()
    queue = bus.subscribe("job:1")
    await bus.publish("job:1", _event("done", 1))

    event = await asyncio.wait_for(queue.get(), timeout=1)
    assert event.event_type == "done"
    sentinel = await asyncio.wait_for(queue.get(), timeout=1)
    assert sentinel is None  # terminal sentinel ends the SSE stream


async def test_subscribe_after_done_replays_then_closes():
    bus = InMemoryEventBus()
    await bus.publish("job:1", _event("queued", 1))
    await bus.publish("job:1", _event("done", 2))

    queue = bus.subscribe("job:1")
    assert (await asyncio.wait_for(queue.get(), timeout=1)).event_type == "queued"
    assert (await asyncio.wait_for(queue.get(), timeout=1)).event_type == "done"
    assert await asyncio.wait_for(queue.get(), timeout=1) is None


async def test_history_reclaimed_after_unsubscribe():
    bus = InMemoryEventBus()
    queue = bus.subscribe("job:1")
    await bus.publish("job:1", _event("done", 1))
    bus.unsubscribe("job:1", queue)
    assert "job:1" not in bus._history
    assert "job:1" not in bus._closed


@pytest.mark.parametrize("limit", [3])
async def test_history_is_bounded(limit):
    bus = InMemoryEventBus(history_limit=limit)
    for i in range(10):
        await bus.publish("job:1", _event("audio_chunk", i))
    assert len(bus._history["job:1"]) == limit
