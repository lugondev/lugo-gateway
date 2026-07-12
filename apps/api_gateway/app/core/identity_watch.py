"""Periodic disabled/revoked re-check for long-lived WS sessions, generalizing
the idle-timeout watchdog pattern already used by app.api.routes.lugo
(lugo_stream's `_watchdog()`): a background asyncio task that wakes on a fixed
interval and checks a condition, closing the connection if it fails."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable


class IdentityWatchdog:
    def __init__(
        self, still_valid: Callable[[], Awaitable[bool]], interval_s: float = 30.0
    ) -> None:
        self._still_valid = still_valid
        self._interval_s = interval_s
        self.invalid = False
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_s)
            if not await self._still_valid():
                self.invalid = True
                return

    def cancel(self) -> None:
        if self.task is not None:
            self.task.cancel()


async def receive_with_watchdog(websocket, watchdog: IdentityWatchdog | None):
    """Async generator yielding websocket.receive() results. If `watchdog` fires
    while waiting, yields None exactly once and stops -- the caller should close
    the connection and break out of its loop."""
    active_watchdog = watchdog
    while True:
        recv = asyncio.create_task(websocket.receive())
        waitables = (
            {recv, active_watchdog.task}
            if active_watchdog is not None and active_watchdog.task is not None
            else {recv}
        )
        await asyncio.wait(waitables, return_when=asyncio.FIRST_COMPLETED)
        if active_watchdog is not None and active_watchdog.invalid:
            recv.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await recv
            yield None
            return
        if recv.done():
            yield recv.result()
            continue
        # The watchdog's task finished for some other reason (e.g. an
        # external .cancel()), not by firing (invalid=True). Stop selecting
        # it in future waits -- otherwise it would keep completing
        # immediately and we'd busy-loop re-checking an already-done task.
        active_watchdog = None
        await recv
        yield recv.result()


async def identity_still_valid(identity) -> bool:
    """identity: app.core.auth_guard.WsIdentity. Standalone so lugo.py's own
    idle-timeout watchdog (Task 16) can call the same check inline, instead of
    running a second concurrent IdentityWatchdog task alongside its existing one."""
    from app.services.auth.devices import device_store
    from app.services.auth.users import user_store

    if identity.user_id is not None:
        user = await user_store.get_by_id(identity.user_id)
        if user is None or user.disabled:
            return False
    if identity.device_id is not None:
        device = await device_store.get_by_id(identity.device_id)
        if device is None or device.revoked:
            return False
    return True


def build_identity_watchdog(identity, interval_s: float = 30.0) -> IdentityWatchdog | None:
    """Returns None when there's nothing to check (the legacy shared
    device_auth_token case has neither user_id nor device_id)."""
    if identity.user_id is None and identity.device_id is None:
        return None
    return IdentityWatchdog(
        still_valid=lambda: identity_still_valid(identity), interval_s=interval_s
    )
