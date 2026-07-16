import asyncio
from collections import defaultdict, deque

from app.schemas.common import TERMINAL_EVENT_TYPES, StreamEvent


class InMemoryEventBus:
    """In-memory pub/sub with per-channel replay and terminal close.

    Replay fixes the race where a producer publishes before the SSE client
    subscribes: a late subscriber receives the buffered history first, then
    live events. Channels are marked closed on a terminal event so subscribers
    can stop.

    Memory is reclaimed in two layers with different lifetimes:

    - History (the heavy part -- up to `history_limit` events per channel) is
      purged when the last subscriber of a closed channel unsubscribes, and
      otherwise by a TTL timer armed at close() -- the common case, since
      every /v1/stt/stream session and /v1/tts/stream job mirrors events here
      and usually no SSE client ever subscribes. The TTL is the replay grace
      window: a subscriber arriving later gets no replay.
    - The closed MARKER (just the channel name) deliberately outlives the
      history: subscribe() relies on it to hand late subscribers an immediate
      end-of-stream sentinel. Without it they'd be registered as live queues
      on a dead channel and their SSE response would hang forever. Markers
      are bounded by `closed_channels_limit` with FIFO eviction.

    Upgrade path: swap this for Redis Pub/Sub + streams when scaling out.
    """

    def __init__(
        self,
        history_limit: int = 1000,
        closed_history_ttl_s: float = 60.0,
        closed_channels_limit: int = 4096,
    ) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[StreamEvent | None]]] = defaultdict(list)
        self._history: dict[str, deque[StreamEvent]] = {}
        # Insertion-ordered so FIFO eviction drops the oldest channel first.
        self._closed: dict[str, None] = {}
        self._history_limit = history_limit
        self._closed_history_ttl_s = closed_history_ttl_s
        self._closed_channels_limit = closed_channels_limit

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
            self._purge_history(channel)

    async def publish(self, channel: str, event: StreamEvent) -> None:
        history = self._history.setdefault(channel, deque(maxlen=self._history_limit))
        history.append(event)
        for queue in list(self._subscribers.get(channel, [])):
            await queue.put(event)
        if event.event_type in TERMINAL_EVENT_TYPES:
            self.close(channel)

    def close(self, channel: str) -> None:
        """Mark a channel terminated and wake subscribers to stop. Idempotent:
        clean STT sessions close twice (terminal publish, then the WS finally
        block) and the second call must not arm a duplicate purge timer."""
        if channel in self._closed:
            return
        self._closed[channel] = None
        while len(self._closed) > self._closed_channels_limit:
            evicted = next(iter(self._closed))
            del self._closed[evicted]
            self._history.pop(evicted, None)
        for queue in list(self._subscribers.get(channel, [])):
            queue.put_nowait(None)
        self._subscribers.pop(channel, None)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop (sync teardown path) -- nothing can subscribe for a
            # replay without a loop either, so reclaim the history now.
            self._purge_history(channel)
            return
        loop.call_later(self._closed_history_ttl_s, self._purge_history, channel)

    def _purge_history(self, channel: str) -> None:
        """Drop a closed channel's replay buffer -- but keep the closed marker
        (see class docstring). Subscribers that attached during the grace
        window replay from their own queue copies, so this is safe while
        they're still draining."""
        if channel in self._closed and not self._subscribers.get(channel):
            self._history.pop(channel, None)


event_bus = InMemoryEventBus()
