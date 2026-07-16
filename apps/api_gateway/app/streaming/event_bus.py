import asyncio
from collections import defaultdict, deque

from app.schemas.common import TERMINAL_EVENT_TYPES, StreamEvent


class InMemoryEventBus:
    """In-memory pub/sub with per-channel replay and terminal close.

    Replay fixes the race where a producer publishes before the SSE client
    subscribes: a late subscriber receives the buffered history first, then
    live events. Channels are marked closed on a terminal event so subscribers
    can stop, and history is reclaimed once no subscribers remain.

    History reclamation happens two ways: immediately when the last subscriber
    of a closed channel unsubscribes, and -- for the common case where NO SSE
    client ever subscribes (every /v1/stt/stream session and /v1/tts/stream
    job mirrors events here unconditionally) -- via a TTL timer armed at
    close(). Without the timer, each finished session leaked up to
    `history_limit` events forever. The TTL is the replay grace window: a
    subscriber arriving later than `closed_history_ttl_s` after the channel
    closed gets nothing.

    Upgrade path: swap this for Redis Pub/Sub + streams when scaling out.
    """

    def __init__(self, history_limit: int = 1000, closed_history_ttl_s: float = 60.0) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[StreamEvent | None]]] = defaultdict(list)
        self._history: dict[str, deque[StreamEvent]] = {}
        self._closed: set[str] = set()
        self._history_limit = history_limit
        self._closed_history_ttl_s = closed_history_ttl_s

    def subscribe(self, channel: str) -> asyncio.Queue[StreamEvent | None]:
        queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()
        # Replay buffered events so no event is missed between produce/subscribe.
        for event in self._history.get(channel, ()):  # type: ignore[arg-type]
            queue.put_nowait(event)
        if channel in self._closed:
            # Producer already finished; signal end-of-stream after replay.
            queue.put_nowait(None)
        else:
            self._subscribers[channel].append(queue)
        return queue

    def unsubscribe(self, channel: str, queue: asyncio.Queue[StreamEvent | None]) -> None:
        subscribers = self._subscribers.get(channel, [])
        if queue in subscribers:
            subscribers.remove(queue)
        if not subscribers:
            self._subscribers.pop(channel, None)
            # Reclaim history once the channel is done and nobody is listening.
            if channel in self._closed:
                self._history.pop(channel, None)
                self._closed.discard(channel)

    async def publish(self, channel: str, event: StreamEvent) -> None:
        history = self._history.setdefault(channel, deque(maxlen=self._history_limit))
        history.append(event)
        for queue in list(self._subscribers.get(channel, [])):
            await queue.put(event)
        if event.event_type in TERMINAL_EVENT_TYPES:
            self.close(channel)

    def close(self, channel: str) -> None:
        """Mark a channel terminated and wake subscribers to stop."""
        self._closed.add(channel)
        for queue in list(self._subscribers.get(channel, [])):
            queue.put_nowait(None)
        self._subscribers.pop(channel, None)
        self._schedule_history_purge(channel)

    def _schedule_history_purge(self, channel: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop (sync teardown path) -- nothing can subscribe for a
            # replay without a loop either, so reclaim immediately.
            self._purge_if_closed(channel)
            return
        loop.call_later(self._closed_history_ttl_s, self._purge_if_closed, channel)

    def _purge_if_closed(self, channel: str) -> None:
        # Subscribers that attached during the grace window replay from their
        # own queue copies (subscribe() never registers queues on a closed
        # channel), so dropping the shared history is safe unconditionally.
        if channel in self._closed:
            self._history.pop(channel, None)
            self._closed.discard(channel)


event_bus = InMemoryEventBus()
