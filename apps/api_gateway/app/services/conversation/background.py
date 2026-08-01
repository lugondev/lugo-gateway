"""Fire-and-forget work a session starts but does not wait for.

Memory extraction, the warm-up notifier, a logged rotation: work whose result
nobody is blocking on, but which must not be lost. Two rules, both learned the
hard way and both easy to get wrong at a call site:

* a task nobody holds a reference to can be garbage-collected mid-flight, so
  every one of them is retained here until it completes;
* they must be given a chance to finish at shutdown, or a server going down
  while a fleet of devices disconnects abandons one memory-extraction write
  per device.

Lives in its own module so those rules are enforced in one place rather than
re-derived next to each ``asyncio.create_task``.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task] = set()


def spawn_background(coro) -> None:
    """Run `coro` detached, retaining it so CPython cannot collect it early."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def drain_background_tasks(timeout: float = 10.0) -> None:
    """Let in-flight background work finish, then give up on the rest.

    Called from the app's shutdown (app/main.py). The work here is memory
    extraction, which runs at the teardown of EVERY session -- so a server going
    down while a fleet of devices disconnects has one of these in flight per
    device, and without this they were simply abandoned mid-write.
    """
    pending = [t for t in _background_tasks if not t.done()]
    if not pending:
        return
    logger.info("draining %d background task(s) before shutdown", len(pending))
    done, still_running = await asyncio.wait(pending, timeout=timeout)
    for task in still_running:
        task.cancel()
    if still_running:
        logger.warning(
            "%d background task(s) did not finish within %.0fs; cancelled",
            len(still_running), timeout,
        )
