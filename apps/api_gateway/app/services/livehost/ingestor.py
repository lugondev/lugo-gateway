"""TikTok Live connection lifecycle: connects, normalizes events onto a shared
queue, and reconnects on failure without ever affecting the rest of the
livehost session (voice keeps working if TikTok drops).

See docs/superpowers/specs/2026-07-05-livehost-tiktok-cohost-design.md section 3.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
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
