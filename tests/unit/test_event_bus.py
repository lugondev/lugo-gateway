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
    # The closed marker must OUTLIVE the history: it's what guarantees a late
    # subscriber gets an immediate end-of-stream sentinel instead of being
    # registered as a live queue on a dead channel (SSE hang).
    assert "job:1" in bus._closed


async def test_closed_channel_history_purged_after_ttl_when_no_subscriber_attaches():
    """The common case for /v1/stt/stream and /v1/tts/stream is that NO SSE
    client ever subscribes to the mirrored channel -- without a TTL purge,
    every finished session/job leaks its full replay history forever."""
    bus = InMemoryEventBus(closed_history_ttl_s=0.05)
    await bus.publish("job:1", _event("done", 1))  # terminal -> closes the channel

    assert "job:1" in bus._history  # replay window still open right after close
    await asyncio.sleep(0.2)
    assert "job:1" not in bus._history
    assert "job:1" in bus._closed  # marker survives so late subscribers still terminate


async def test_explicit_close_without_terminal_event_purges_history_after_ttl():
    """STT WS teardown calls event_bus.close() directly (routes/stt.py) --
    channels that end without a terminal event must be reclaimed too."""
    bus = InMemoryEventBus(closed_history_ttl_s=0.05)
    await bus.publish("job:1", _event("partial_transcript", 1))
    bus.close("job:1")

    await asyncio.sleep(0.2)
    assert "job:1" not in bus._history
    assert "job:1" in bus._closed


async def test_subscriber_arriving_after_ttl_purge_gets_immediate_end_of_stream():
    """Regression: the TTL purge used to drop the _closed marker too, so a
    subscriber arriving later than the grace window (page reload, SSE
    reconnect after a sleep) was registered as a live queue on a channel
    nothing would ever publish to again -- the SSE response hung forever."""
    bus = InMemoryEventBus(closed_history_ttl_s=0.05)
    await bus.publish("job:1", _event("done", 1))
    await asyncio.sleep(0.2)  # TTL purge has fired

    queue = bus.subscribe("job:1")
    sentinel = await asyncio.wait_for(queue.get(), timeout=1)
    assert sentinel is None  # immediate clean end-of-stream, no replay, NO hang
    assert not bus._subscribers.get("job:1")  # not registered as a live queue


async def test_close_is_idempotent_and_arms_a_single_purge_timer():
    """Every clean STT session double-closes: publish('done') closes via
    TERMINAL_EVENT_TYPES, then the WS finally block calls close() again.
    The second close must be a no-op (one sentinel, one purge timer)."""
    bus = InMemoryEventBus(closed_history_ttl_s=60.0)
    queue = bus.subscribe("job:1")
    bus.close("job:1")
    bus.close("job:1")

    assert queue.get_nowait() is None
    assert queue.empty()  # exactly one sentinel despite two close() calls


async def test_closed_markers_are_bounded_fifo():
    bus = InMemoryEventBus(closed_channels_limit=3)
    for i in range(5):
        await bus.publish(f"job:{i}", _event("done", 1))

    assert len(bus._closed) == 3
    assert "job:0" not in bus._closed  # oldest evicted first
    assert "job:4" in bus._closed


async def test_late_subscriber_within_ttl_still_gets_replay():
    bus = InMemoryEventBus(closed_history_ttl_s=5.0)
    await bus.publish("job:1", _event("queued", 1))
    await bus.publish("job:1", _event("done", 2))

    queue = bus.subscribe("job:1")
    assert (await asyncio.wait_for(queue.get(), timeout=1)).event_type == "queued"
    assert (await asyncio.wait_for(queue.get(), timeout=1)).event_type == "done"
    assert await asyncio.wait_for(queue.get(), timeout=1) is None


async def test_ttl_purge_does_not_touch_channels_with_live_subscribers():
    bus = InMemoryEventBus(closed_history_ttl_s=0.05)
    queue = bus.subscribe("job:1")
    await bus.publish("job:1", _event("audio_chunk", 1))  # not terminal, stays open

    await asyncio.sleep(0.2)
    assert "job:1" in bus._history  # open channel with a subscriber is untouched
    bus.unsubscribe("job:1", queue)


@pytest.mark.parametrize("limit", [3])
async def test_history_is_bounded(limit):
    bus = InMemoryEventBus(history_limit=limit)
    for i in range(10):
        await bus.publish("job:1", _event("audio_chunk", i))
    assert len(bus._history["job:1"]) == limit
