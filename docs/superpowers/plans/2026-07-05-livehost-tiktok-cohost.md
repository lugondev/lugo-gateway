# Livehost: TikTok Live AI Co-host Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `livehost` feature that runs an AI co-host for a TikTok livestream — it answers viewer comments/gifts by voice (LLM + TTS) and handles the streamer's own spoken input (STT), reusing the existing Conversation infrastructure (STT/TTS services, responder, VAD endpointer, session history, system/profile config) instead of duplicating it.

**Architecture:** New `app/services/livehost/` package (SocialEvent schema, EventScheduler priority queue, TikTokLiveIngestor + adapter, LiveHostOrchestrator arbiter) plus a new `app/api/routes/livehost.py` WS+REST route. `conversation.py`/`responder.py` are read-only dependencies — nothing there is modified.

**Tech Stack:** FastAPI (WebSocket + REST), `TikTokLive` python library (unofficial TikTok Live client) as a new optional dependency, existing `stt_service`/`tts_service`/`responder`/`session_store`/`profile_store`/`system_config_store`.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-05-livehost-tiktok-cohost-design.md` — every task below implements one section of it.
- v1 scope explicitly excludes (do not build): tool-calling/MCP registry, memory injection, fast-path STT routing, streaming STT, audio-native (Qwen-Omni) replies, and virtual-audio-cable/OBS output — the livehost WS route reuses the *voice contract* from `/v1/conversation/stream` (audio codec, event names) but not those extra features. If a task below seems to need one of these, it doesn't — keep it out.
- Single TikTok room per livehost session (1 session ↔ at most 1 `TikTokLiveIngestor`). No multi-room, no multi-platform.
- Losing the TikTok connection must never crash or pause the WS session's voice handling — this is enforced by construction (the ingestor and the voice turn loop are independent tasks) and must hold in every task that touches the ingestor.
- New pydantic-settings fields belong in `apps/api_gateway/app/core/settings.py` next to the existing `conversation_*` block, following its naming/comment style.
- Tests run via `pytest` from repo root (`pythonpath = ["apps/api_gateway"]`, `asyncio_mode = "auto"` — no `@pytest.mark.asyncio` needed, see `pyproject.toml`).
- Every commit is scoped to the task that produced it; run `pytest tests/unit tests/integration -q` before each commit if you touched more than the current task's own test file.

---

### Task 1: SocialEvent schema + livehost settings

**Files:**
- Create: `apps/api_gateway/app/services/livehost/__init__.py` (empty)
- Create: `apps/api_gateway/app/services/livehost/schemas.py`
- Modify: `apps/api_gateway/app/core/settings.py:218` (right after the `conversation_tool_max_iters: int = 3` line, before the next blank-line section)
- Test: `tests/unit/test_livehost_schemas.py`

**Interfaces:**
- Produces: `SocialEvent` (pydantic `BaseModel`) with fields `id: str`, `platform: Literal["tiktok"] = "tiktok"`, `kind: Literal["comment", "gift", "like", "follow", "share"]`, `user_id: str`, `user_name: str`, `user_avatar_url: str | None = None`, `text: str | None = None`, `gift_name: str | None = None`, `gift_value: int | None = None`, `like_count: int | None = None`, `timestamp: float`.
- Produces on `settings`: `livehost_mention_keywords: str`, `livehost_individual_threshold: int`, `livehost_batch_top_k: int`, `livehost_queue_max_size: int`, `livehost_backoff_initial_seconds: float`, `livehost_backoff_max_seconds: float`, `livehost_offline_poll_interval_seconds: float`, `livehost_watchdog_idle_seconds: float`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_livehost_schemas.py
from app.services.livehost.schemas import SocialEvent


def test_social_event_requires_kind_and_user():
    event = SocialEvent(
        id="e1", kind="comment", user_id="u1", user_name="Alice", text="hi", timestamp=1.0,
    )
    assert event.platform == "tiktok"
    assert event.gift_value is None


def test_social_event_rejects_unknown_kind():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SocialEvent(id="e1", kind="not-a-kind", user_id="u1", user_name="Alice", timestamp=1.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_livehost_schemas.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.livehost'`

- [ ] **Step 3: Write the schema**

```python
# apps/api_gateway/app/services/livehost/schemas.py
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class SocialEvent(BaseModel):
    """A single normalized event from a social live-stream platform (comment,
    gift, like, follow, share). Platform-specific ingestors (TikTokLiveIngestor
    etc.) translate their native event shapes into this one."""

    id: str
    platform: Literal["tiktok"] = "tiktok"
    kind: Literal["comment", "gift", "like", "follow", "share"]
    user_id: str
    user_name: str
    user_avatar_url: str | None = None
    text: str | None = None
    gift_name: str | None = None
    gift_value: int | None = None
    like_count: int | None = None
    timestamp: float
```

```python
# apps/api_gateway/app/services/livehost/__init__.py
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_livehost_schemas.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Add livehost settings**

Open `apps/api_gateway/app/core/settings.py`, find this existing line (around line 218):

```python
    conversation_tool_max_iters: int = 3
```

Add immediately after it:

```python

    # Livehost: TikTok Live AI co-host (see docs/superpowers/specs/2026-07-05-livehost-tiktok-cohost-design.md).
    # Comma-separated keywords that boost a comment's reply priority (e.g. bot name).
    livehost_mention_keywords: str = ""
    # Backlog size at/under which the scheduler replies to events one at a time.
    livehost_individual_threshold: int = 3
    # Above the threshold, how many top-priority events to fold into one batch reply.
    livehost_batch_top_k: int = 3
    # Hard cap on pending events; lowest-priority non-gift/non-mention entries are
    # dropped first once exceeded.
    livehost_queue_max_size: int = 200
    # TikTok ingestor reconnect backoff (transient errors): starts here, doubles up
    # to the max, plus jitter.
    livehost_backoff_initial_seconds: float = 1.0
    livehost_backoff_max_seconds: float = 60.0
    # How often to re-check whether an offline room has gone live again.
    livehost_offline_poll_interval_seconds: float = 30.0
    # Force-reconnect if no event arrives for this long while state is "live" (a
    # connection that died without a clean disconnect signal).
    livehost_watchdog_idle_seconds: float = 300.0
```

- [ ] **Step 6: Run the full unit test suite to confirm nothing else broke**

Run: `pytest tests/unit -q`
Expected: PASS, no failures

- [ ] **Step 7: Commit**

```bash
git add apps/api_gateway/app/services/livehost/__init__.py apps/api_gateway/app/services/livehost/schemas.py apps/api_gateway/app/core/settings.py tests/unit/test_livehost_schemas.py
git commit -m "feat(livehost): add SocialEvent schema and livehost settings"
```

---

### Task 2: EventScheduler — priority queue + adaptive batching

**Files:**
- Create: `apps/api_gateway/app/services/livehost/scheduler.py`
- Test: `tests/unit/test_livehost_scheduler.py`

**Interfaces:**
- Consumes: `SocialEvent` from Task 1 (`app.services.livehost.schemas`).
- Produces: `SocialTurn` dataclass (`events: list[SocialEvent]`, `overflow_count: int`); `score_event(event: SocialEvent, mention_keywords: list[str]) -> float`; `EventScheduler` class with `__init__(self, mention_keywords: list[str] | None = None, individual_threshold: int = 3, batch_top_k: int = 3, max_queue_size: int = 200)`, `enqueue(event: SocialEvent) -> None`, `has_pending(self) -> bool`, `pending_count(self) -> int`, `next_turn(self) -> SocialTurn | None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_livehost_scheduler.py
from app.services.livehost.schemas import SocialEvent
from app.services.livehost.scheduler import EventScheduler, score_event


def _event(kind="comment", **kwargs) -> SocialEvent:
    defaults = dict(id="e", user_id="u", user_name="user", timestamp=1.0)
    defaults.update(kwargs)
    return SocialEvent(kind=kind, **defaults)


def test_gift_scores_higher_than_plain_comment():
    gift = _event(kind="gift", gift_name="Rose", gift_value=50)
    comment = _event(kind="comment", text="hello")
    assert score_event(gift, []) > score_event(comment, [])


def test_mention_keyword_boosts_comment_above_gift():
    mention = _event(kind="comment", text="hey CoHostBot answer me")
    gift = _event(kind="gift", gift_name="Rose", gift_value=5000)
    assert score_event(mention, ["CoHostBot"]) > score_event(gift, ["CoHostBot"])


def test_like_scores_lowest_by_default():
    like = _event(kind="like", like_count=20)
    comment = _event(kind="comment", text="hi")
    assert score_event(like, []) < score_event(comment, [])


def test_small_backlog_returns_single_event_turn():
    scheduler = EventScheduler(individual_threshold=3, batch_top_k=3)
    scheduler.enqueue(_event(id="e1", text="first"))
    scheduler.enqueue(_event(id="e2", text="second"))

    turn = scheduler.next_turn()

    assert turn is not None
    assert len(turn.events) == 1
    assert turn.overflow_count == 0
    # The remaining event is still pending for the next call.
    assert scheduler.pending_count() == 1


def test_large_backlog_batches_top_k_and_reports_overflow():
    scheduler = EventScheduler(individual_threshold=2, batch_top_k=2)
    for i in range(5):
        scheduler.enqueue(_event(id=f"e{i}", text=f"msg {i}"))

    turn = scheduler.next_turn()

    assert turn is not None
    assert len(turn.events) == 2
    assert turn.overflow_count == 3
    assert scheduler.pending_count() == 0  # batch clears the whole backlog


def test_next_turn_on_empty_queue_returns_none():
    scheduler = EventScheduler()
    assert scheduler.next_turn() is None
    assert scheduler.has_pending() is False


def test_queue_cap_drops_lowest_priority_before_gifts():
    scheduler = EventScheduler(max_queue_size=3, individual_threshold=0, batch_top_k=0)
    scheduler.enqueue(_event(id="gift1", kind="gift", gift_name="Rose", gift_value=10))
    scheduler.enqueue(_event(id="like1", kind="like", like_count=1))
    scheduler.enqueue(_event(id="like2", kind="like", like_count=1))
    # Exceeds cap of 3 -> must drop a "like", never the gift.
    scheduler.enqueue(_event(id="like3", kind="like", like_count=1))

    remaining_ids = {s.event.id for s in scheduler._queue}  # noqa: SLF001 - white-box test
    assert "gift1" in remaining_ids
    assert len(remaining_ids) == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_livehost_scheduler.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.livehost.scheduler'`

- [ ] **Step 3: Implement the scheduler**

```python
# apps/api_gateway/app/services/livehost/scheduler.py
"""Priority queue for social-live events (comments/gifts/likes/follows/shares).

Dequeue decisions (single event vs. batch) are made at consumption time
(`next_turn`), not at enqueue time, since the backlog size the decision should
react to keeps changing between calls. See
docs/superpowers/specs/2026-07-05-livehost-tiktok-cohost-design.md section 3.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.livehost.schemas import SocialEvent

_KIND_BASE_SCORE = {
    "gift": 500.0,
    "follow": 200.0,
    "share": 100.0,
    "comment": 100.0,
    "like": 0.0,
}
# Large enough that a keyword-mention comment always outranks even a big gift
# (real TikTok gifts top out in the tens of thousands of diamonds per send) —
# mention is priority tier 1, gift is tier 2, per the design doc's ordered list.
_MENTION_BONUS = 100_000.0
# Entries scoring at/above this are never evicted by the queue cap (covers all
# gifts and any mention-boosted comment).
PROTECTED_SCORE_FLOOR = 500.0


def score_event(event: SocialEvent, mention_keywords: list[str]) -> float:
    score = _KIND_BASE_SCORE.get(event.kind, 0.0)
    if event.kind == "gift" and event.gift_value:
        score += float(event.gift_value)
    if event.text and mention_keywords:
        lowered = event.text.lower()
        if any(kw.lower() in lowered for kw in mention_keywords if kw):
            score += _MENTION_BONUS
    return score


@dataclass
class _ScoredEvent:
    event: SocialEvent
    score: float


@dataclass
class SocialTurn:
    events: list[SocialEvent]
    overflow_count: int = 0


class EventScheduler:
    """Not thread-safe beyond a single asyncio event loop; drive it from one
    orchestrator/session only."""

    def __init__(
        self,
        mention_keywords: list[str] | None = None,
        individual_threshold: int = 3,
        batch_top_k: int = 3,
        max_queue_size: int = 200,
    ) -> None:
        self.mention_keywords = mention_keywords or []
        self.individual_threshold = individual_threshold
        self.batch_top_k = batch_top_k
        self.max_queue_size = max_queue_size
        self._queue: list[_ScoredEvent] = []

    def enqueue(self, event: SocialEvent) -> None:
        scored = _ScoredEvent(event=event, score=score_event(event, self.mention_keywords))
        self._queue.append(scored)
        self._queue.sort(key=lambda s: s.score, reverse=True)
        if len(self._queue) > self.max_queue_size:
            self._evict_one()

    def _evict_one(self) -> None:
        for i in range(len(self._queue) - 1, -1, -1):
            candidate = self._queue[i]
            if candidate.event.kind != "gift" and candidate.score < PROTECTED_SCORE_FLOOR:
                del self._queue[i]
                return
        # Every pending entry is protected (gift/mention) but we're still over
        # cap -> drop the lowest-scored one rather than growing unbounded.
        self._queue.pop()

    def has_pending(self) -> bool:
        return bool(self._queue)

    def pending_count(self) -> int:
        return len(self._queue)

    def next_turn(self) -> SocialTurn | None:
        if not self._queue:
            return None
        if len(self._queue) <= self.individual_threshold:
            top = self._queue.pop(0)
            return SocialTurn(events=[top.event], overflow_count=0)

        top_k = self._queue[: self.batch_top_k]
        overflow = len(self._queue) - len(top_k)
        events = [s.event for s in top_k]
        self._queue.clear()
        return SocialTurn(events=events, overflow_count=overflow)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_livehost_scheduler.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/livehost/scheduler.py tests/unit/test_livehost_scheduler.py
git commit -m "feat(livehost): add EventScheduler with priority scoring and adaptive batching"
```

---

### Task 3: TikTokLiveIngestor — connection state machine + reconnect

**Files:**
- Create: `apps/api_gateway/app/services/livehost/ingestor.py`
- Test: `tests/unit/test_livehost_ingestor.py`

**Interfaces:**
- Consumes: `SocialEvent` (Task 1).
- Produces: `IngestorState` (`str` enum: `IDLE`, `CONNECTING`, `LIVE`, `RECONNECTING`, `OFFLINE_WAITING`, `ERROR`); `RoomOfflineError(Exception)`; `TikTokLiveIngestor` with `__init__(self, client_factory: Callable[[str], Any], queue: asyncio.Queue, backoff_initial: float = 1.0, backoff_max: float = 60.0, offline_poll_interval: float = 30.0, watchdog_idle_seconds: float = 300.0)`, `async def start(self, unique_id: str) -> None`, `async def stop(self) -> None`, `.state: IngestorState`, `.unique_id: str | None`.
- The `client_factory(unique_id)` must return an object with `async def connect(self) -> None` (raises `RoomOfflineError` if the room isn't live, any other exception on transient failure), `def events(self) -> AsyncIterator[SocialEvent | None]` (an item of `None` signals a clean disconnect — reconnect immediately without waiting out the watchdog), and `async def close(self) -> None`. Task 4's `TikTokLiveClientAdapter` implements this protocol against the real TikTok library; here it's satisfied by a `FakeClient` test double.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_livehost_ingestor.py
import asyncio

import pytest

from app.services.livehost.ingestor import IngestorState, RoomOfflineError, TikTokLiveIngestor
from app.services.livehost.schemas import SocialEvent


def _event(i: int) -> SocialEvent:
    return SocialEvent(id=f"e{i}", kind="comment", user_id="u", user_name="user", text=str(i), timestamp=float(i))


class FakeClient:
    """Test double for the connect()/events()/close() protocol.

    ``script`` is a list of behaviors consumed one per connect() attempt:
    - a list[SocialEvent | None] -> connect succeeds, events() yields them then hangs
      (simulating a still-open connection) unless the list ends with None (clean
      disconnect) or the test cancels it.
    - an Exception instance -> connect() raises it.
    """

    instances: list["FakeClient"] = []

    def __init__(self, unique_id: str, script: list) -> None:
        self.unique_id = unique_id
        self._script = script
        self.closed = False
        FakeClient.instances.append(self)

    async def connect(self) -> None:
        behavior = self._script.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        self._events = behavior

    async def _event_gen(self):
        for item in self._events:
            yield item
        # Simulate a connection that just sits open with no more events, so the
        # watchdog (not StopAsyncIteration) is what ends it.
        await asyncio.sleep(3600)

    def events(self):
        return self._event_gen()

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_fake_client():
    FakeClient.instances.clear()
    yield
    FakeClient.instances.clear()


async def test_start_connects_and_forwards_events_to_queue():
    scripts = [[_event(1), _event(2)]]
    factory = lambda uid: FakeClient(uid, scripts.pop(0))
    queue: asyncio.Queue = asyncio.Queue()
    ingestor = TikTokLiveIngestor(client_factory=factory, queue=queue, watchdog_idle_seconds=3600)

    await ingestor.start("alice")
    first = await asyncio.wait_for(queue.get(), timeout=1)
    second = await asyncio.wait_for(queue.get(), timeout=1)

    assert ingestor.state == IngestorState.LIVE
    assert [first.text, second.text] == ["1", "2"]

    await ingestor.stop()
    assert ingestor.state == IngestorState.IDLE
    assert FakeClient.instances[0].closed is True


async def test_room_offline_polls_slowly_then_recovers():
    scripts = [RoomOfflineError("offline"), [_event(1)]]
    factory = lambda uid: FakeClient(uid, [scripts.pop(0)])
    queue: asyncio.Queue = asyncio.Queue()
    ingestor = TikTokLiveIngestor(
        client_factory=factory, queue=queue, offline_poll_interval=0.01, watchdog_idle_seconds=3600,
    )

    await ingestor.start("alice")
    await asyncio.sleep(0.02)
    assert ingestor.state in (IngestorState.OFFLINE_WAITING, IngestorState.LIVE)

    event = await asyncio.wait_for(queue.get(), timeout=1)
    assert event.text == "1"
    assert ingestor.state == IngestorState.LIVE
    await ingestor.stop()


async def test_transient_error_backs_off_then_reconnects():
    scripts = [RuntimeError("network blip"), [_event(1)]]
    factory = lambda uid: FakeClient(uid, [scripts.pop(0)])
    queue: asyncio.Queue = asyncio.Queue()
    ingestor = TikTokLiveIngestor(
        client_factory=factory, queue=queue,
        backoff_initial=0.01, backoff_max=0.02, watchdog_idle_seconds=3600,
    )

    await ingestor.start("alice")
    event = await asyncio.wait_for(queue.get(), timeout=1)
    assert event.text == "1"
    await ingestor.stop()


async def test_clean_disconnect_signal_reconnects_immediately():
    scripts = [[_event(1), None], [_event(2)]]
    factory = lambda uid: FakeClient(uid, [scripts.pop(0)])
    queue: asyncio.Queue = asyncio.Queue()
    ingestor = TikTokLiveIngestor(
        client_factory=factory, queue=queue, backoff_initial=0.01, watchdog_idle_seconds=3600,
    )

    await ingestor.start("alice")
    first = await asyncio.wait_for(queue.get(), timeout=1)
    second = await asyncio.wait_for(queue.get(), timeout=1)
    assert [first.text, second.text] == ["1", "2"]
    await ingestor.stop()


async def test_stale_connection_forces_reconnect_via_watchdog():
    scripts = [[_event(1)], [_event(2)]]
    factory = lambda uid: FakeClient(uid, [scripts.pop(0)])
    queue: asyncio.Queue = asyncio.Queue()
    ingestor = TikTokLiveIngestor(
        client_factory=factory, queue=queue, backoff_initial=0.01, watchdog_idle_seconds=0.05,
    )

    await ingestor.start("alice")
    first = await asyncio.wait_for(queue.get(), timeout=1)
    second = await asyncio.wait_for(queue.get(), timeout=2)
    assert [first.text, second.text] == ["1", "2"]
    assert FakeClient.instances[0].closed is True
    await ingestor.stop()


async def test_starting_twice_stops_the_previous_connection():
    scripts = [[_event(1)], [_event(2)]]
    factory = lambda uid: FakeClient(uid, [scripts.pop(0)])
    queue: asyncio.Queue = asyncio.Queue()
    ingestor = TikTokLiveIngestor(client_factory=factory, queue=queue, watchdog_idle_seconds=3600)

    await ingestor.start("alice")
    await asyncio.wait_for(queue.get(), timeout=1)
    await ingestor.start("bob")
    event = await asyncio.wait_for(queue.get(), timeout=1)

    assert event.text == "2"
    assert FakeClient.instances[0].closed is True
    assert len(FakeClient.instances) == 2
    await ingestor.stop()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_livehost_ingestor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.livehost.ingestor'`

- [ ] **Step 3: Implement the ingestor**

```python
# apps/api_gateway/app/services/livehost/ingestor.py
"""TikTok Live connection lifecycle: connects, normalizes events onto a shared
queue, and reconnects on failure without ever affecting the rest of the
livehost session (voice keeps working if TikTok drops).

See docs/superpowers/specs/2026-07-05-livehost-tiktok-cohost-design.md section 3.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any, Protocol

from app.services.livehost.schemas import SocialEvent

logger = logging.getLogger(__name__)


class IngestorState(str, Enum):
    IDLE = "idle"
    CONNECTING = "connecting"
    LIVE = "live"
    RECONNECTING = "reconnecting"
    OFFLINE_WAITING = "offline_waiting"
    ERROR = "error"


class RoomOfflineError(Exception):
    """Raised by a client's connect() when the target room isn't currently live."""


class LiveClientProtocol(Protocol):
    async def connect(self) -> None: ...
    def events(self) -> Any: ...  # AsyncIterator[SocialEvent | None]
    async def close(self) -> None: ...


class TikTokLiveIngestor:
    def __init__(
        self,
        client_factory: Callable[[str], LiveClientProtocol],
        queue: "asyncio.Queue[SocialEvent]",
        backoff_initial: float = 1.0,
        backoff_max: float = 60.0,
        offline_poll_interval: float = 30.0,
        watchdog_idle_seconds: float = 300.0,
    ) -> None:
        self._client_factory = client_factory
        self.queue = queue
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self.offline_poll_interval = offline_poll_interval
        self.watchdog_idle_seconds = watchdog_idle_seconds

        self.state = IngestorState.IDLE
        self.unique_id: str | None = None
        self._task: asyncio.Task | None = None
        self._generation = 0
        self._stop_requested = False
        self._lock = asyncio.Lock()

    async def start(self, unique_id: str) -> None:
        async with self._lock:
            await self._stop_locked()
            self.unique_id = unique_id
            self._stop_requested = False
            self._generation += 1
            self.state = IngestorState.CONNECTING
            self._task = asyncio.create_task(self._run(self._generation))

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        self._stop_requested = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - teardown must not raise
                pass
        self.state = IngestorState.IDLE
        self._task = None

    async def _run(self, generation: int) -> None:
        backoff = self.backoff_initial
        while not self._stop_requested and generation == self._generation:
            client = self._client_factory(self.unique_id)
            try:
                self.state = (
                    IngestorState.CONNECTING if backoff == self.backoff_initial else IngestorState.RECONNECTING
                )
                await client.connect()
            except RoomOfflineError:
                self.state = IngestorState.OFFLINE_WAITING
                await asyncio.sleep(self.offline_poll_interval)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - transient connect failure
                logger.warning("tiktok ingestor connect failed for %s: %s", self.unique_id, exc)
                self.state = IngestorState.ERROR
                await self._sleep_backoff(backoff)
                backoff = min(backoff * 2, self.backoff_max)
                continue

            self.state = IngestorState.LIVE
            backoff = self.backoff_initial
            stale = await self._drain(client, generation)

            if self._stop_requested or generation != self._generation:
                return
            if stale:
                continue  # reconnect immediately, no backoff for a stale (not failed) link
            self.state = IngestorState.RECONNECTING
            await self._sleep_backoff(backoff)
            backoff = min(backoff * 2, self.backoff_max)

    async def _drain(self, client: LiveClientProtocol, generation: int) -> bool:
        """Pull events from *client* into self.queue until it disconnects, errors,
        or goes stale. Returns True if it ended because of watchdog staleness."""
        events_iter = client.events().__aiter__()
        stale = False
        try:
            while True:
                try:
                    raw_event = await asyncio.wait_for(events_iter.__anext__(), timeout=self.watchdog_idle_seconds)
                except asyncio.TimeoutError:
                    logger.warning("tiktok ingestor stale for %s, forcing reconnect", self.unique_id)
                    stale = True
                    break
                except StopAsyncIteration:
                    break
                if raw_event is None:  # adapter's clean-disconnect signal
                    break
                await self.queue.put(raw_event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - transient mid-stream error
            logger.warning("tiktok ingestor stream error for %s: %s", self.unique_id, exc)
        finally:
            try:
                await client.close()
            except Exception:  # noqa: BLE001 - teardown must not raise
                pass
        return stale

    async def _sleep_backoff(self, backoff: float) -> None:
        jitter = random.uniform(0, backoff * 0.25)
        await asyncio.sleep(backoff + jitter)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_livehost_ingestor.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/livehost/ingestor.py tests/unit/test_livehost_ingestor.py
git commit -m "feat(livehost): add TikTokLiveIngestor with backoff/offline/watchdog reconnect"
```

---

### Task 4: TikTokLiveClientAdapter — real TikTokLive library wiring

**Files:**
- Create: `apps/api_gateway/app/services/livehost/tiktok_adapter.py`
- Modify: `pyproject.toml` (new optional dependency group)
- Test: `tests/unit/test_livehost_tiktok_adapter.py`

**Interfaces:**
- Consumes: `SocialEvent` (Task 1), `RoomOfflineError` (Task 3).
- Produces: `TikTokLiveClientAdapter` implementing Task 3's `LiveClientProtocol` (`connect`/`events`/`close`), constructible as `TikTokLiveClientAdapter(unique_id)` — usable directly as the `client_factory` callable passed to `TikTokLiveIngestor`. Also exports pure mapping helpers `map_comment`, `map_gift`, `map_like`, `map_follow`, `map_share`, `avatar_url` for testing without the real `TikTokLive` library types.

This task depends on the real `TikTokLive` PyPI package (v6.6.5 confirmed during design): `TikTokLiveClient(unique_id=...)`, `await client.start(fetch_live_check=True)` (raises `TikTokLive.client.errors.UserOfflineError` / `UserNotFoundError`, returns once connected — the WS pump keeps running as a background task on the client), `client.on(EventType, async_handler)`, `await client.disconnect(close_client=True)`. Event shapes: `CommentEvent.user` / `.comment`, `GiftEvent.user` / `.gift.name` / `.gift.diamond_count` / `.repeat_count` / `.streaking`, `LikeEvent.user` / `.count`, `FollowEvent.user`, `ShareEvent.user`. `user.unique_id` / `user.nickname` are properties on `ExtendedUser`; avatar is `user.avatar_thumb.m_urls[0]` if present.

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, after the existing `qwen3-asr-cuda` extra block, add:

```toml
# TikTok Live co-host ingestion (unofficial API). See
# docs/superpowers/specs/2026-07-05-livehost-tiktok-cohost-design.md.
tiktok = [
  "TikTokLive>=6.6.0",
]
```

Run: `pip install -e ".[tiktok,dev]"`
Expected: installs successfully (network required — this mirrors how `mlx`/`opus` extras already work).

- [ ] **Step 2: Write the failing tests (pure mapping helpers only — no real TikTokLive types)**

```python
# tests/unit/test_livehost_tiktok_adapter.py
from types import SimpleNamespace

from app.services.livehost.tiktok_adapter import (
    avatar_url,
    map_comment,
    map_follow,
    map_gift,
    map_like,
    map_share,
)


def _user(unique_id="alice", nickname="Alice", avatar=None):
    thumb = SimpleNamespace(m_urls=[avatar]) if avatar else None
    return SimpleNamespace(unique_id=unique_id, nickname=nickname, avatar_thumb=thumb)


def test_avatar_url_returns_first_url_or_none():
    assert avatar_url(_user(avatar="http://x/a.png")) == "http://x/a.png"
    assert avatar_url(_user(avatar=None)) is None


def test_map_comment():
    event = SimpleNamespace(user=_user(), comment="hello there")
    social = map_comment(event)
    assert social.kind == "comment"
    assert social.user_id == "alice"
    assert social.user_name == "Alice"
    assert social.text == "hello there"


def test_map_gift_skips_ongoing_streak():
    streaking_event = SimpleNamespace(
        user=_user(), streaking=True, repeat_count=3,
        gift=SimpleNamespace(name="Rose", diamond_count=1),
    )
    assert map_gift(streaking_event) is None

    finished_event = SimpleNamespace(
        user=_user(), streaking=False, repeat_count=3,
        gift=SimpleNamespace(name="Rose", diamond_count=1),
    )
    social = map_gift(finished_event)
    assert social.kind == "gift"
    assert social.gift_name == "Rose"
    assert social.gift_value == 3


def test_map_like():
    event = SimpleNamespace(user=_user(), count=7)
    social = map_like(event)
    assert social.kind == "like"
    assert social.like_count == 7


def test_map_follow_and_share():
    event = SimpleNamespace(user=_user())
    assert map_follow(event).kind == "follow"
    assert map_share(event).kind == "share"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/unit/test_livehost_tiktok_adapter.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.livehost.tiktok_adapter'`

- [ ] **Step 4: Implement the adapter**

```python
# apps/api_gateway/app/services/livehost/tiktok_adapter.py
"""Adapts the real TikTokLive client's callback API to the connect()/events()/
close() protocol TikTokLiveIngestor expects (see ingestor.LiveClientProtocol),
normalizing its event objects into SocialEvent.

Mapping helpers (map_comment etc.) are pure and duck-typed so they're unit
testable without the real TikTokLive proto classes; only TikTokLiveClientAdapter
itself touches the actual library.
"""

from __future__ import annotations

import asyncio
import time
import uuid

from app.services.livehost.ingestor import RoomOfflineError
from app.services.livehost.schemas import SocialEvent


def avatar_url(user) -> str | None:
    thumb = getattr(user, "avatar_thumb", None)
    urls = getattr(thumb, "m_urls", None) if thumb else None
    return urls[0] if urls else None


def map_comment(event) -> SocialEvent:
    return SocialEvent(
        id=str(uuid.uuid4()), kind="comment",
        user_id=event.user.unique_id, user_name=event.user.nickname,
        user_avatar_url=avatar_url(event.user), text=event.comment,
        timestamp=time.time(),
    )


def map_gift(event) -> SocialEvent | None:
    if event.streaking:
        return None  # wait for the streak to finish so gift_value is final
    return SocialEvent(
        id=str(uuid.uuid4()), kind="gift",
        user_id=event.user.unique_id, user_name=event.user.nickname,
        user_avatar_url=avatar_url(event.user), gift_name=event.gift.name,
        gift_value=event.repeat_count * event.gift.diamond_count,
        timestamp=time.time(),
    )


def map_like(event) -> SocialEvent:
    return SocialEvent(
        id=str(uuid.uuid4()), kind="like",
        user_id=event.user.unique_id, user_name=event.user.nickname,
        user_avatar_url=avatar_url(event.user), like_count=event.count,
        timestamp=time.time(),
    )


def map_follow(event) -> SocialEvent:
    return SocialEvent(
        id=str(uuid.uuid4()), kind="follow",
        user_id=event.user.unique_id, user_name=event.user.nickname,
        user_avatar_url=avatar_url(event.user), timestamp=time.time(),
    )


def map_share(event) -> SocialEvent:
    return SocialEvent(
        id=str(uuid.uuid4()), kind="share",
        user_id=event.user.unique_id, user_name=event.user.nickname,
        user_avatar_url=avatar_url(event.user), timestamp=time.time(),
    )


class TikTokLiveClientAdapter:
    def __init__(self, unique_id: str) -> None:
        from TikTokLive import TikTokLiveClient

        self._client = TikTokLiveClient(unique_id=unique_id)
        self._queue: asyncio.Queue = asyncio.Queue()
        self._register_handlers()

    def _register_handlers(self) -> None:
        from TikTokLive.events import CommentEvent, FollowEvent, GiftEvent, LikeEvent, ShareEvent

        self._client.on(CommentEvent, self._on_comment)
        self._client.on(GiftEvent, self._on_gift)
        self._client.on(LikeEvent, self._on_like)
        self._client.on(FollowEvent, self._on_follow)
        self._client.on(ShareEvent, self._on_share)

        from TikTokLive.events.custom_events import DisconnectEvent, LiveEndEvent

        self._client.on(DisconnectEvent, self._on_disconnect)
        self._client.on(LiveEndEvent, self._on_disconnect)

    async def _on_comment(self, event) -> None:
        await self._queue.put(map_comment(event))

    async def _on_gift(self, event) -> None:
        mapped = map_gift(event)
        if mapped is not None:
            await self._queue.put(mapped)

    async def _on_like(self, event) -> None:
        await self._queue.put(map_like(event))

    async def _on_follow(self, event) -> None:
        await self._queue.put(map_follow(event))

    async def _on_share(self, event) -> None:
        await self._queue.put(map_share(event))

    async def _on_disconnect(self, event) -> None:
        await self._queue.put(None)  # signals TikTokLiveIngestor to reconnect immediately

    async def connect(self) -> None:
        from TikTokLive.client.errors import UserNotFoundError, UserOfflineError

        try:
            await self._client.start(fetch_live_check=True)
        except (UserOfflineError, UserNotFoundError) as exc:
            raise RoomOfflineError(str(exc)) from exc

    async def events(self):
        while True:
            yield await self._queue.get()

    async def close(self) -> None:
        await self._client.disconnect(close_client=True)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_livehost_tiktok_adapter.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/services/livehost/tiktok_adapter.py pyproject.toml tests/unit/test_livehost_tiktok_adapter.py
git commit -m "feat(livehost): add TikTokLiveClientAdapter wiring the real TikTokLive library"
```

**Note for whoever runs this task:** the real `TikTokLiveClient` connection (`connect()`/`_on_*` callbacks firing from an actual TikTok room) cannot be unit tested offline — it needs a live TikTok room. The mapping helpers above are fully covered; the wiring itself should be smoke-tested manually against a real or test TikTok room once Task 7's REST `connect` endpoint exists.

---

### Task 5: LiveHostOrchestrator — social-turn arbitration

**Files:**
- Create: `apps/api_gateway/app/services/livehost/orchestrator.py`
- Test: `tests/unit/test_livehost_orchestrator.py`

**Interfaces:**
- Consumes: `EventScheduler`, `SocialTurn` (Task 2).
- Produces: `format_social_turn(turn: SocialTurn) -> str`; `LiveHostOrchestrator` with `__init__(self, scheduler: EventScheduler)` and `poll_social_turn(self, voice_active: bool) -> tuple[SocialTurn, str] | None` — returns `None` if the streamer is currently talking (voice always wins) or nothing is pending; otherwise dequeues one turn from the scheduler and returns it alongside its formatted chat-history text.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_livehost_orchestrator.py
from app.services.livehost.orchestrator import LiveHostOrchestrator, format_social_turn
from app.services.livehost.scheduler import EventScheduler, SocialTurn
from app.services.livehost.schemas import SocialEvent


def _event(kind="comment", **kwargs) -> SocialEvent:
    defaults = dict(id="e", user_id="u1", user_name="Bao", timestamp=1.0)
    defaults.update(kwargs)
    return SocialEvent(kind=kind, **defaults)


def test_format_comment_turn():
    turn = SocialTurn(events=[_event(text="xin chao")])
    text = format_social_turn(turn)
    assert "@Bao" in text
    assert "xin chao" in text


def test_format_gift_turn():
    turn = SocialTurn(events=[_event(kind="gift", gift_name="Rose", gift_value=50)])
    text = format_social_turn(turn)
    assert "Rose" in text
    assert "50" in text


def test_format_batch_turn_mentions_overflow():
    turn = SocialTurn(events=[_event(text="a"), _event(text="b")], overflow_count=12)
    text = format_social_turn(turn)
    assert "a" in text and "b" in text
    assert "12" in text


def test_poll_returns_none_while_voice_active():
    scheduler = EventScheduler()
    scheduler.enqueue(_event(text="hi"))
    orchestrator = LiveHostOrchestrator(scheduler)

    assert orchestrator.poll_social_turn(voice_active=True) is None
    assert scheduler.pending_count() == 1  # nothing was dequeued


def test_poll_returns_none_when_nothing_pending():
    orchestrator = LiveHostOrchestrator(EventScheduler())
    assert orchestrator.poll_social_turn(voice_active=False) is None


def test_poll_dequeues_and_formats_when_voice_idle():
    scheduler = EventScheduler(individual_threshold=5)
    scheduler.enqueue(_event(text="hi"))
    orchestrator = LiveHostOrchestrator(scheduler)

    result = orchestrator.poll_social_turn(voice_active=False)

    assert result is not None
    turn, text = result
    assert len(turn.events) == 1
    assert "hi" in text
    assert scheduler.pending_count() == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_livehost_orchestrator.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.livehost.orchestrator'`

- [ ] **Step 3: Implement the orchestrator**

```python
# apps/api_gateway/app/services/livehost/orchestrator.py
"""Turn arbitration between the streamer's voice (always wins) and pending
social events. Purely decision + text-formatting logic — actually running a
turn (LLM + TTS + sending WS events) is the WS route's job (it already knows
how to do that for voice turns; social turns reuse the same machinery), so
this stays a small, independently testable arbiter.
"""

from __future__ import annotations

from app.services.livehost.scheduler import EventScheduler, SocialTurn


def format_social_turn(turn: SocialTurn) -> str:
    lines: list[str] = []
    for event in turn.events:
        if event.kind == "comment":
            lines.append(f"[TikTok @{event.user_name}]: {event.text}")
        elif event.kind == "gift":
            lines.append(f"[TikTok @{event.user_name}] sent a gift: {event.gift_name} (value {event.gift_value})")
        elif event.kind == "follow":
            lines.append(f"[TikTok @{event.user_name}] just followed the stream")
        elif event.kind == "share":
            lines.append(f"[TikTok @{event.user_name}] shared the stream")
        elif event.kind == "like":
            lines.append(f"[TikTok] {event.like_count or 0} new likes")
    if turn.overflow_count:
        lines.append(f"(and {turn.overflow_count} more from other viewers)")
    return "\n".join(lines)


class LiveHostOrchestrator:
    def __init__(self, scheduler: EventScheduler) -> None:
        self.scheduler = scheduler

    def poll_social_turn(self, voice_active: bool) -> tuple[SocialTurn, str] | None:
        if voice_active or not self.scheduler.has_pending():
            return None
        turn = self.scheduler.next_turn()
        if turn is None:
            return None
        return turn, format_social_turn(turn)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_livehost_orchestrator.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/livehost/orchestrator.py tests/unit/test_livehost_orchestrator.py
git commit -m "feat(livehost): add LiveHostOrchestrator turn arbitration"
```

---

### Task 6: Session registry + WS route `/v1/livehost/stream` (voice turns)

**Files:**
- Create: `apps/api_gateway/app/services/livehost/registry.py`
- Create: `apps/api_gateway/app/api/routes/livehost.py`
- Modify: `apps/api_gateway/app/main.py` (register the router — see Task 7, done together since the router must be registered to be reachable by tests)
- Test: `tests/integration/test_livehost_ws_voice.py`

This task wires up the WS route with **voice-only** turn handling (VAD → STT → LLM → TTS → audio out), reusing the same building blocks `conversation.py` uses (`VadEndpointer`, `stt_service`, `tts_service`, `build_responder_ex`, `resolve_system_prompt`, `session_store`, Opus codec helpers, `prefetch_synthesis`/`pacing_delays`). Per the Global Constraints, no tool-calling, no memory injection, no fast-path STT, no audio-native — those stay out of scope. The social side (scheduler/orchestrator/ingestor wiring) is added in Task 7.

**Interfaces:**
- Consumes: `EventScheduler` (Task 2), `TikTokLiveIngestor` (Task 3) — registry holds them per session but this task doesn't wire ingestion yet.
- Produces: `LivehostSession` dataclass (`scheduler: EventScheduler`, `ingestor: TikTokLiveIngestor`), `LivehostSessionRegistry` with `register`, `get`, `unregister`; module-level `livehost_registry` singleton; `router` (FastAPI `APIRouter`, prefix `/v1/livehost`) with `WS /stream`.

- [ ] **Step 1: Implement the session registry**

```python
# apps/api_gateway/app/services/livehost/registry.py
"""In-memory registry of active livehost WS sessions, keyed by session_id, so
the REST connect/disconnect/status endpoints (Task 7) can reach the right
session's TikTokLiveIngestor. Entries are created when a WS session starts and
removed when it ends."""

from __future__ import annotations

from dataclasses import dataclass

from app.services.livehost.ingestor import TikTokLiveIngestor
from app.services.livehost.scheduler import EventScheduler


@dataclass
class LivehostSession:
    scheduler: EventScheduler
    ingestor: TikTokLiveIngestor


class LivehostSessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, LivehostSession] = {}

    def register(self, session_id: str, session: LivehostSession) -> None:
        self._sessions[session_id] = session

    def get(self, session_id: str) -> LivehostSession | None:
        return self._sessions.get(session_id)

    def unregister(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


livehost_registry = LivehostSessionRegistry()
```

- [ ] **Step 2: Write the failing integration test (voice-only path)**

```python
# tests/integration/test_livehost_ws_voice.py
import asyncio

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.schemas.stt import STTResult
from app.schemas.tts import TTSResult
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service

SR = 16000


class _StubSTT(STTProvider):
    name = "stub-livehost"

    async def transcribe_bytes(self, audio_bytes, language=None) -> STTResult:
        return STTResult(engine=self.name, text="chao ban", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-livehost-tts"

    async def synthesize(self, payload) -> TTSResult:
        return TTSResult(
            engine=self.name, sample_rate=24000, audio_url="/artifacts/x.wav",
            duration_seconds=0.1, text=payload.text, mock=True,
        )


@pytest.fixture(autouse=True)
def _register_stub(monkeypatch):
    monkeypatch.setattr(settings, "enable_mock_engines", True)
    monkeypatch.setattr(settings, "conversation_llm_base_url", "")
    stt_service.providers["stub-livehost"] = _StubSTT()
    tts_service.providers["stub-livehost-tts"] = _StubTTS()
    yield
    stt_service.providers.pop("stub-livehost", None)
    tts_service.providers.pop("stub-livehost-tts", None)


def _loud(ms: int) -> bytes:
    n = int(SR * ms / 1000)
    return (np.full(n, 0.2, dtype=np.float32) * 32767).astype("<i2").tobytes()


def _silence(ms: int) -> bytes:
    return (b"\x00\x00") * int(SR * ms / 1000)


def test_livehost_voice_turn_end_to_end():
    client = TestClient(app)
    url = "/v1/livehost/stream?stt_engine=stub-livehost&tts_engine=stub-livehost-tts&sample_rate=16000"
    with client.websocket_connect(url) as ws:
        started = ws.receive_json()
        assert started["event"] == "session_started"
        session_id = started["session_id"]

        ws.send_bytes(_loud(500))
        ws.send_bytes(_silence(500))
        ws.send_bytes(_silence(500))

        events = []
        for _ in range(20):
            ev = ws.receive_json()
            events.append(ev)
            if ev["event"] == "turn_done":
                break

        kinds = [e["event"] for e in events]
        assert "user_transcript" in kinds
        assert "audio_chunk" in kinds
        assert kinds[-1] == "turn_done"

    from app.services.livehost.registry import livehost_registry
    assert livehost_registry.get(session_id) is None  # cleaned up on disconnect
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/integration/test_livehost_ws_voice.py -v`
Expected: FAIL with `404 Not Found` (route doesn't exist yet) or `ModuleNotFoundError`

- [ ] **Step 4: Implement the WS route (voice-only for now)**

```python
# apps/api_gateway/app/api/routes/livehost.py
import asyncio
import json
import logging
import uuid
from contextlib import aclosing

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from app.core.audio import pcm16_to_wav_bytes, wav_file_to_pcm16
from app.core.errors import AppError
from app.core.settings import settings
from app.schemas.tts import TTSRequest
from app.services.conversation.endpointer import VadEndpointer
from app.services.conversation.responder import build_responder_ex
from app.services.history.store import session_store
from app.services.livehost.ingestor import TikTokLiveIngestor
from app.services.livehost.registry import LivehostSession, livehost_registry
from app.services.livehost.scheduler import EventScheduler
from app.services.profiles.store import profile_store
from app.services.stt.service import stt_service
from app.services.tts.service import tts_service
from app.services.tts.streaming import pacing_delays, prefetch_synthesis
from app.services.warmup import is_ready, warm_providers

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/livehost", tags=["livehost"])


def _mention_keywords() -> list[str]:
    return [k.strip() for k in settings.livehost_mention_keywords.split(",") if k.strip()]


@router.websocket("/stream")
async def livehost_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    session_id = websocket.query_params.get("session_id") or str(uuid.uuid4())
    q = websocket.query_params

    profile_name = q.get("profile")
    profile = profile_store.get(profile_name) if profile_name else None
    llm_base_url = (profile.llm.base_url or None) if (profile and profile.llm.base_url) else None
    llm_api_key = profile.llm.api_key if (profile and profile.llm.base_url) else None
    llm_model = (profile.llm.model or None) if (profile and profile.llm.model) else None
    system_prompt = (profile.system_prompt or None) if (profile and profile.system_prompt) else None

    stt_engine = q.get("stt_engine") or settings.conversation_stt_engine or settings.default_stt_engine
    language = q.get("language") or settings.conversation_language or None
    if profile and profile.tts.engine:
        tts_engine = profile.tts.engine
        voice = profile.tts.voice or q.get("voice") or None
    else:
        tts_engine = q.get("tts_engine") or settings.conversation_tts_engine or settings.default_tts_engine
        voice = q.get("voice") or None
    sample_rate = int(q.get("sample_rate", settings.stt_stream_sample_rate))
    audio_codec = (q.get("audio_codec") or "pcm16").lower()
    out_modalities = {m.strip() for m in (q.get("output") or "audio,text").lower().split(",") if m.strip()}
    want_audio = "audio" in out_modalities
    want_text = "text" in out_modalities
    audio_out = (q.get("audio_out") or "url").lower()
    output_sample_rate = int(q.get("output_sample_rate", 24000))

    try:
        stt_provider = stt_service.get_provider(stt_engine)
        tts_provider = tts_service.get_provider(tts_engine)
    except AppError as exc:
        await websocket.send_json({"event": "error", "message": str(exc)})
        await websocket.close()
        return

    opus_decoder = None
    if audio_codec == "opus":
        from app.core.opus import OpusFrameDecoder, opus_available

        if opus_available():
            opus_decoder = OpusFrameDecoder(sample_rate=sample_rate, channels=1)
        else:
            audio_codec = "pcm16"
            logger.warning("client requested opus but server has no libopus; using pcm16")

    opus_encoder = None
    if want_audio and audio_out == "opus":
        from app.core.opus import OpusFrameEncoder, opus_available

        if opus_available():
            opus_encoder = OpusFrameEncoder(sample_rate=output_sample_rate, channels=1)
        else:
            audio_out = "url"
            logger.warning("client requested opus output but server has no libopus; using url")

    responder = build_responder_ex(
        base_url=llm_base_url, api_key=llm_api_key, model=llm_model, system_prompt=system_prompt,
    )

    endpointer = VadEndpointer(
        sample_rate,
        silence_ms=settings.conversation_silence_ms,
        min_speech_ms=settings.conversation_min_speech_ms,
        rms_threshold=settings.conversation_rms_threshold,
        max_utterance_ms=settings.conversation_max_utterance_ms,
        min_silence_ms=settings.conversation_min_silence_ms,
        adaptive_full_ms=settings.conversation_adaptive_full_ms,
    )

    history: list[dict] = []
    session_ready = True
    try:
        await session_store.create(
            session_id, profile_id=profile_name or "",
            meta={"stt_engine": stt_engine, "tts_engine": tts_engine, "livehost": True},
        )
    except Exception as exc:  # noqa: BLE001 - session setup must not drop the connection
        logger.warning("livehost session setup failed for %s: %s", session_id, exc)
        session_ready = False
    turn = 0

    raw_social_queue: asyncio.Queue = asyncio.Queue()
    scheduler = EventScheduler(
        mention_keywords=_mention_keywords(),
        individual_threshold=settings.livehost_individual_threshold,
        batch_top_k=settings.livehost_batch_top_k,
        max_queue_size=settings.livehost_queue_max_size,
    )
    ingestor = TikTokLiveIngestor(
        client_factory=_default_tiktok_client_factory,
        queue=raw_social_queue,
        backoff_initial=settings.livehost_backoff_initial_seconds,
        backoff_max=settings.livehost_backoff_max_seconds,
        offline_poll_interval=settings.livehost_offline_poll_interval_seconds,
        watchdog_idle_seconds=settings.livehost_watchdog_idle_seconds,
    )
    livehost_registry.register(session_id, LivehostSession(scheduler=scheduler, ingestor=ingestor))

    stt_ready = is_ready(stt_provider)
    tts_ready = is_ready(tts_provider)
    await websocket.send_json({
        "event": "session_started",
        "session_id": session_id,
        "profile": profile_name,
        "stt_engine": stt_engine,
        "tts_engine": tts_engine,
        "responder": responder.name,
        "sample_rate": sample_rate,
        "audio_codec": audio_codec,
        "output": sorted(out_modalities),
        "audio_out": audio_out,
        "output_sample_rate": output_sample_rate if want_audio and audio_out != "url" else None,
        "stt_ready": stt_ready,
        "tts_ready": tts_ready,
    })

    async def _warm_and_notify() -> None:
        await warm_providers(tts_provider, stt_provider)
        if not (stt_ready and tts_ready):
            try:
                await websocket.send_json({"event": "engines_ready"})
            except Exception:  # noqa: BLE001 - socket may already be closed/gone
                pass

    asyncio.create_task(_warm_and_notify())

    async def send(event: str, **payload) -> None:
        await websocket.send_json({"event": event, **payload})

    async def persist(role: str, content: str) -> None:
        if not session_ready:
            return
        try:
            await session_store.append_message(session_id, turn, role, content)
        except Exception as exc:  # noqa: BLE001 - persistence must not kill the turn
            logger.warning("livehost history persist failed: %s", exc)

    async def _stream_to_tts(sentence_aiter, responder_name: str) -> list[str]:
        parts: list[str] = []
        if not want_audio:
            index = 0
            async for sentence in sentence_aiter:
                parts.append(sentence)
                if want_text:
                    await send("response_text", turn=turn, chunk_index=index, text=sentence, responder=responder_name)
                index += 1
            return parts

        async def _synth(sentence: str):
            result = await tts_provider.synthesize(TTSRequest(text=sentence, engine=tts_engine, voice=voice))
            if opus_encoder is not None:
                path = result.audio_url.lstrip("/")
                pcm = await asyncio.to_thread(wav_file_to_pcm16, path, output_sample_rate)
                packets = await asyncio.to_thread(opus_encoder.encode_pcm16, pcm)
                return result, packets
            return result, None

        async with aclosing(
            prefetch_synthesis(sentence_aiter, _synth, lookahead=settings.conversation_tts_lookahead)
        ) as pipeline:
            async for index, sentence, (result, packets) in pipeline:
                parts.append(sentence)
                if want_text:
                    await send("response_text", turn=turn, chunk_index=index, text=sentence, responder=responder_name)
                if packets is not None:
                    await send(
                        "audio_start", turn=turn, chunk_index=index,
                        text=sentence if want_text else None,
                        codec="opus", sample_rate=output_sample_rate, frames=len(packets),
                    )
                    if settings.conversation_opus_pace and packets:
                        frame_s = opus_encoder.frame / opus_encoder.sample_rate
                        delays = pacing_delays(len(packets), settings.conversation_opus_prebuffer_frames, frame_s)
                    else:
                        delays = [0.0] * len(packets)
                    for delay, pkt in zip(delays, packets):
                        if delay:
                            await asyncio.sleep(delay)
                        await websocket.send_bytes(pkt)
                    await send("audio_end", turn=turn, chunk_index=index)
                else:
                    await send(
                        "audio_chunk", turn=turn, chunk_index=index, text=sentence,
                        audio_url=result.audio_url, sample_rate=result.sample_rate, mock=result.mock,
                    )
        return parts

    async def _run_voice_turn(audio_pcm: bytes) -> None:
        nonlocal turn
        turn += 1
        await send("processing", turn=turn)
        wav = pcm16_to_wav_bytes(audio_pcm, sample_rate=sample_rate)
        try:
            stt_result = await stt_provider.transcribe_bytes(wav, language)
        except RuntimeError as exc:
            await send("error", message=f"STT failed: {exc}")
            return
        user_text = (stt_result.text or "").strip()
        await send("user_transcript", turn=turn, text=user_text, engine=stt_engine)
        if not user_text:
            await send("turn_done", turn=turn, skipped="empty transcript")
            return

        history.append({"role": "user", "content": user_text})
        await persist("user", user_text)
        parts = await _stream_to_tts(responder.reply_stream(history), responder.name)
        history.append({"role": "assistant", "content": " ".join(parts)})
        await persist("assistant", " ".join(parts))
        await send("turn_done", turn=turn)

    async def run_voice_turn(audio_pcm: bytes) -> None:
        # Per the spec, a voice-turn failure must surface to the streamer directly
        # (unlike a social-turn failure, which is just logged and dropped — see
        # run_social_turn) so the session never hangs waiting for a turn_done that
        # was lost to an uncaught exception inside the background task.
        try:
            await _run_voice_turn(audio_pcm)
        except Exception as exc:  # noqa: BLE001 - keep the session alive
            logger.exception("livehost voice turn failed")
            await send("error", message=str(exc))
            await send("turn_done", turn=turn)

    current_turn: asyncio.Task | None = None

    async def abort_turn(reason: str) -> None:
        nonlocal current_turn
        if current_turn and not current_turn.done():
            current_turn.cancel()
            try:
                await current_turn
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            await send("aborted", reason=reason)
        current_turn = None

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break

            if message.get("bytes") is not None:
                frame = message["bytes"]
                if opus_decoder is not None:
                    try:
                        frame = opus_decoder.decode(frame)
                    except Exception as exc:  # noqa: BLE001 - skip a bad packet, keep going
                        logger.warning("livehost opus decode failed: %s", exc)
                        continue
                event = endpointer.accept(frame)
                if not event:
                    continue
                if event["event"] == "speech_start":
                    await abort_turn("barge-in")
                    await send("speech_start")
                elif event["event"] == "endpoint":
                    await abort_turn("superseded")
                    await send("speech_end", speech_ms=round(event["speech_ms"]))
                    current_turn = asyncio.create_task(run_voice_turn(event["audio"]))

            if message.get("text") is not None:
                try:
                    control = json.loads(message["text"])
                except json.JSONDecodeError:
                    control = {}
                ctype = control.get("type")
                if ctype == "abort":
                    await abort_turn("user")
                elif ctype == "reset":
                    await abort_turn("reset")
                    history.clear()
                    endpointer.reset()
                    await send("reset")
                elif ctype in {"flush", "end"}:
                    audio = endpointer.flush()
                    if audio:
                        await abort_turn("superseded")
                        await send("speech_end", speech_ms=0)
                        current_turn = asyncio.create_task(run_voice_turn(audio))
                    if ctype == "end":
                        await abort_turn("end")
                        await send("done")
                        break
    except WebSocketDisconnect:
        pass
    finally:
        if current_turn and not current_turn.done():
            current_turn.cancel()
        await ingestor.stop()
        livehost_registry.unregister(session_id)
        if session_ready:
            try:
                await session_store.mark_ended(session_id)
            except Exception as exc:  # noqa: BLE001 - teardown must not fail
                logger.warning("livehost mark_ended failed for %s: %s", session_id, exc)
        try:
            await websocket.close()
        except RuntimeError:
            pass


def _default_tiktok_client_factory(unique_id: str):
    from app.services.livehost.tiktok_adapter import TikTokLiveClientAdapter

    return TikTokLiveClientAdapter(unique_id)
```

Register the router — modify `apps/api_gateway/app/main.py`:

```python
# after this existing line (17):
from app.api.routes.health import router as health_router
# add:
from app.api.routes.livehost import router as livehost_router
```

```python
# after this existing line (119 in the current file: app.include_router(system_router)):
app.include_router(system_router)
# add:
app.include_router(livehost_router)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/integration/test_livehost_ws_voice.py -v`
Expected: PASS (1 test)

- [ ] **Step 6: Run the full test suite**

Run: `pytest tests/unit tests/integration -q`
Expected: PASS, no regressions in existing conversation tests

- [ ] **Step 7: Commit**

```bash
git add apps/api_gateway/app/services/livehost/registry.py apps/api_gateway/app/api/routes/livehost.py apps/api_gateway/app/main.py tests/integration/test_livehost_ws_voice.py
git commit -m "feat(livehost): add /v1/livehost/stream WS route with voice-only turns"
```

---

### Task 7: Wire social turns into the WS route + REST connect/disconnect/status

**Files:**
- Modify: `apps/api_gateway/app/api/routes/livehost.py`
- Test: `tests/integration/test_livehost_ws_social.py`

This is the task that makes the feature actually a "co-host": a background task drains `raw_social_queue` into the `scheduler` (emitting `social_event` immediately for each), and a second background task polls `LiveHostOrchestrator.poll_social_turn(voice_active=...)` whenever the streamer is silent and no turn is in flight, running a social turn through the same `_stream_to_tts` pipeline as voice turns. REST endpoints let a client attach/detach/inspect the TikTok ingestor for a session.

**Interfaces:**
- Consumes: `LiveHostOrchestrator` (Task 5), `LivehostSession`/`livehost_registry` (Task 6).
- Produces: `POST /v1/livehost/{session_id}/connect {unique_id: str}`, `POST /v1/livehost/{session_id}/disconnect`, `GET /v1/livehost/{session_id}/status`.

- [ ] **Step 1: Write the failing integration test**

```python
# tests/integration/test_livehost_ws_social.py
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.schemas.stt import STTResult
from app.schemas.tts import TTSResult
from app.services.livehost.registry import livehost_registry
from app.services.livehost.schemas import SocialEvent
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service


class _StubSTT(STTProvider):
    name = "stub-livehost-social"

    async def transcribe_bytes(self, audio_bytes, language=None) -> STTResult:
        return STTResult(engine=self.name, text="", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-livehost-social-tts"

    async def synthesize(self, payload) -> TTSResult:
        return TTSResult(
            engine=self.name, sample_rate=24000, audio_url="/artifacts/x.wav",
            duration_seconds=0.05, text=payload.text, mock=True,
        )


@pytest.fixture(autouse=True)
def _register_stub(monkeypatch):
    monkeypatch.setattr(settings, "enable_mock_engines", True)
    monkeypatch.setattr(settings, "conversation_llm_base_url", "")
    monkeypatch.setattr(settings, "livehost_individual_threshold", 5)
    stt_service.providers["stub-livehost-social"] = _StubSTT()
    tts_service.providers["stub-livehost-social-tts"] = _StubTTS()
    yield
    stt_service.providers.pop("stub-livehost-social", None)
    tts_service.providers.pop("stub-livehost-social-tts", None)


def test_social_event_triggers_reply_when_streamer_silent():
    client = TestClient(app)
    url = "/v1/livehost/stream?stt_engine=stub-livehost-social&tts_engine=stub-livehost-social-tts"
    with client.websocket_connect(url) as ws:
        started = ws.receive_json()
        session_id = started["session_id"]

        session = livehost_registry.get(session_id)
        assert session is not None
        session.scheduler.enqueue(
            SocialEvent(id="e1", kind="comment", user_id="u1", user_name="Bao", text="hello!", timestamp=1.0)
        )

        events = []
        for _ in range(20):
            ev = ws.receive_json()
            events.append(ev)
            if ev["event"] == "turn_done":
                break

        kinds = [e["event"] for e in events]
        assert "social_reply" in kinds
        assert "audio_chunk" in kinds


class _FakeTikTokClient:
    """Stands in for TikTokLiveClientAdapter so this test never touches the
    real network — it exercises the connect/disconnect/status wiring only."""

    def __init__(self, unique_id: str) -> None:
        self.unique_id = unique_id

    async def connect(self) -> None:
        await asyncio.sleep(3600)  # "stays live" for the duration of the test

    def events(self):
        async def _gen():
            await asyncio.sleep(3600)
            yield None  # pragma: no cover - unreachable, keeps this an async generator

        return _gen()

    async def close(self) -> None:
        pass


def test_connect_disconnect_status_endpoints(monkeypatch):
    monkeypatch.setattr(
        "app.api.routes.livehost._default_tiktok_client_factory",
        lambda unique_id: _FakeTikTokClient(unique_id),
    )
    client = TestClient(app)
    with client.websocket_connect("/v1/livehost/stream?stt_engine=stub-livehost-social&tts_engine=stub-livehost-social-tts") as ws:
        session_id = ws.receive_json()["session_id"]

        status = client.get(f"/v1/livehost/{session_id}/status").json()
        assert status["data"]["state"] == "idle"

        resp = client.post(f"/v1/livehost/{session_id}/connect", json={"unique_id": "some_streamer"})
        assert resp.status_code == 200
        assert resp.json()["data"]["unique_id"] == "some_streamer"

        client.post(f"/v1/livehost/{session_id}/disconnect")
        status = client.get(f"/v1/livehost/{session_id}/status").json()
        assert status["data"]["state"] == "idle"


def test_status_for_unknown_session_is_404():
    client = TestClient(app)
    resp = client.get("/v1/livehost/does-not-exist/status")
    assert resp.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/integration/test_livehost_ws_social.py -v`
Expected: FAIL — `social_reply` never appears (nothing drains the scheduler yet); connect/disconnect/status return 404 for everything (routes don't exist)

- [ ] **Step 3: Add the social-turn draining/polling loops and REST endpoints**

In `apps/api_gateway/app/api/routes/livehost.py`, add the import:

```python
from pydantic import BaseModel

from app.services.livehost.orchestrator import LiveHostOrchestrator
```

Add a request model and the three REST endpoints (place them above the `@router.websocket("/stream")` def):

```python
class TikTokConnectRequest(BaseModel):
    unique_id: str


@router.post("/{session_id}/connect")
async def connect_tiktok(session_id: str, payload: TikTokConnectRequest) -> dict:
    session = livehost_registry.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"livehost session '{session_id}' not found")
    await session.ingestor.start(payload.unique_id)
    return {"success": True, "data": {"state": session.ingestor.state.value, "unique_id": payload.unique_id}}


@router.post("/{session_id}/disconnect")
async def disconnect_tiktok(session_id: str) -> dict:
    session = livehost_registry.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"livehost session '{session_id}' not found")
    await session.ingestor.stop()
    return {"success": True, "data": {"state": session.ingestor.state.value}}


@router.get("/{session_id}/status")
async def livehost_status(session_id: str) -> dict:
    session = livehost_registry.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"livehost session '{session_id}' not found")
    return {
        "success": True,
        "data": {
            "state": session.ingestor.state.value,
            "unique_id": session.ingestor.unique_id,
            "pending_social_events": session.scheduler.pending_count(),
        },
    }
```

Add `run_social_turn` next to `run_voice_turn` (same scope, so it can reuse `_stream_to_tts`/`persist`/`send`/`turn`):

```python
    async def _run_social_turn(social_turn, formatted_text: str) -> None:
        nonlocal turn
        turn += 1
        await send(
            "social_reply", turn=turn,
            event_count=len(social_turn.events), overflow_count=social_turn.overflow_count,
        )
        history.append({"role": "user", "content": formatted_text})
        await persist("user", formatted_text)
        parts = await _stream_to_tts(responder.reply_stream(history), responder.name)
        history.append({"role": "assistant", "content": " ".join(parts)})
        await persist("assistant", " ".join(parts))
        await send("turn_done", turn=turn)

    async def run_social_turn(social_turn, formatted_text: str) -> None:
        # Per the spec: a social-turn failure is logged and the event is dropped,
        # never surfaced as a hard error to the streamer — unlike run_voice_turn.
        try:
            await _run_social_turn(social_turn, formatted_text)
        except Exception:  # noqa: BLE001 - drop this social turn, keep the session alive
            logger.exception("livehost social turn failed, dropping event")
```

Inside `livehost_stream`, right after the `livehost_registry.register(...)` line, construct the orchestrator and start the two background loops. `_poll_social_turns` assigns the social turn to `current_turn` (not just `await`s it inline) specifically so a voice `speech_start` arriving mid-reply can still barge in via the existing `abort_turn()` — the `voice_active` check on each poll tick already prevents it from starting a *second* social turn while one is in flight, since `current_turn.done()` will be `False` until that turn finishes or is cancelled:

```python
    orchestrator = LiveHostOrchestrator(scheduler)

    async def _drain_social_events() -> None:
        while True:
            event = await raw_social_queue.get()
            scheduler.enqueue(event)
            try:
                await send(
                    "social_event", kind=event.kind, user_name=event.user_name,
                    user_avatar_url=event.user_avatar_url, text=event.text,
                    gift_name=event.gift_name, gift_value=event.gift_value,
                )
            except Exception:  # noqa: BLE001 - socket may already be closed
                return

    async def _poll_social_turns() -> None:
        nonlocal current_turn
        while True:
            await asyncio.sleep(0.5)
            voice_active = endpointer.speaking or (current_turn is not None and not current_turn.done())
            result = orchestrator.poll_social_turn(voice_active=voice_active)
            if result is None:
                continue
            social_turn, formatted_text = result
            current_turn = asyncio.create_task(run_social_turn(social_turn, formatted_text))

    drain_task = asyncio.create_task(_drain_social_events())
    poll_task = asyncio.create_task(_poll_social_turns())
```

`current_turn` is declared with `current_turn: asyncio.Task | None = None` earlier in `livehost_stream` (Task 6) — `_poll_social_turns` closes over that same variable via `nonlocal`, exactly like `run_voice_turn`'s dispatch site does.

Finally, cancel both background tasks in the `finally` block (alongside the existing `current_turn` cancellation and `ingestor.stop()`):

```python
    finally:
        if current_turn and not current_turn.done():
            current_turn.cancel()
        drain_task.cancel()
        poll_task.cancel()
        await ingestor.stop()
        livehost_registry.unregister(session_id)
        ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/integration/test_livehost_ws_social.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the full test suite**

Run: `pytest tests/unit tests/integration -q`
Expected: PASS, no regressions

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/api/routes/livehost.py tests/integration/test_livehost_ws_social.py
git commit -m "feat(livehost): drain social events into replies, add connect/disconnect/status endpoints"
```

---

## After this plan

Not covered here (see spec's "out of scope" section — future work, not gaps in this plan):
- Virtual-audio-cable / OBS integration so the AI voice reaches the real broadcast.
- Manual smoke test of `TikTokLiveClientAdapter` against a real TikTok room (flagged in Task 4).
- Tool-calling, memory injection, multi-room/multi-platform support.
