"""Coarse in-process sliding-window rate limiter.

Lifted out of app.services.auth.pairing (where it was `_RateLimiter`, private
to the device-pairing flow) so the password endpoints can use the same one --
see api/routes/auth.py. Two changes on the way out:

* stale keys are swept, so the map cannot grow without bound. The original
  used a `defaultdict(deque)` and `allow()` minted an entry for every key it
  was ever asked about, keeping it forever; every key is derived from
  attacker-varied input (a client IP, a username), so that map was a slow
  memory leak an attacker could drive.
* `charge()` is separate from `allow()`, so a caller can check the budget
  without spending it. Login only charges FAILED attempts -- charging
  successes too would let anyone lock a user out of their own account by
  burning the budget for them.

Process-local state, same caveat the pairing module already documented: with
more than one uvicorn worker or replica each process keeps its own counters,
so an attacker landing requests on N processes gets N x the budget. Fine for
this single-worker deployment; a multi-worker rollout needs a shared store
(e.g. Redis) to keep the guarantee.
"""

from __future__ import annotations

import time
from collections import deque

# How often a call may trigger a full sweep of stale keys. Cheap enough to do
# inline (the map holds thousands of entries at worst, and a sweep is a scan of
# short deques) and rare enough that it never sits on the hot path.
_SWEEP_INTERVAL_S = 60.0


class SlidingWindowRateLimiter:
    def __init__(self, max_events: int, window_seconds: float) -> None:
        self._max_events = max_events
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._last_sweep = time.monotonic()

    def _prune(self, key: str, now: float) -> deque[float]:
        hits = self._hits.get(key)
        if hits is None:
            return deque()
        while hits and now - hits[0] > self._window:
            hits.popleft()
        return hits

    def _sweep(self, now: float) -> None:
        """Drop keys whose window has fully drained. Without this the map keeps
        one entry per distinct key forever -- and keys come from client IPs and
        submitted usernames, i.e. from the attacker."""
        if now - self._last_sweep < _SWEEP_INTERVAL_S:
            return
        self._last_sweep = now
        for key in list(self._hits):
            if not self._prune(key, now):
                self._hits.pop(key, None)

    def allow(self, key: str) -> bool:
        """True if `key` is under its limit, charging one event. The
        check-and-spend shape the pairing routes have always used."""
        if not self.check(key):
            return False
        self.charge(key)
        return True

    def check(self, key: str) -> bool:
        """True if `key` is under its limit. Spends nothing."""
        now = time.monotonic()
        self._sweep(now)
        return len(self._prune(key, now)) < self._max_events

    def charge(self, key: str) -> None:
        """Record one event against `key`, whether or not it was under limit."""
        now = time.monotonic()
        self._sweep(now)
        hits = self._hits.get(key)
        if hits is None:
            hits = self._hits[key] = deque()
        else:
            self._prune(key, now)
        hits.append(now)

    def reset(self) -> None:
        """Test-only: clear all tracked keys."""
        self._hits.clear()
        self._last_sweep = time.monotonic()
