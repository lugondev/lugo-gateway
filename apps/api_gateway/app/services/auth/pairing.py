"""In-memory pending-device-pairing registry -- same pattern as
app.services.livehost.registry.livehost_registry (a process-global dict is
fine here: entries are short-lived (~10 min TTL) and losing them on restart
just means the device retries pair/init).

--- C3 hardening -----------------------------------------------------------
A 6-digit numeric code (1e6 space), a 600s TTL, and no rate limiting anywhere
made `pair/claim` brute-forceable: an *authenticated* attacker could submit
codes until one hit, claiming a victim's device onto the attacker's account
(the victim's conversations then land under the attacker's user_id). Three
independent layers close this:

1. Burn-after-N (primary defense against a sweep already in flight when a
   pairing exists -- `_MAX_CLAIM_ATTEMPTS`, `record_failed_attempt`): every
   failed claim (a code that matches no live pairing) counts as a strike
   against every pairing that already existed *before the current miss
   streak began*. Tolerates 5 wrong guesses; the 6th burns it -- deleting it
   outright, so even the *correct* code then fails. In the normal case there
   is 0 or 1 pairing outstanding at a time (a device inits, displays its
   code, and is claimed within minutes), so this reduces to "6 wrong
   guesses burns the one pairing that was already in flight."

   Round 1 shipped this as "charge *every* outstanding pairing on *every*
   miss," full stop -- which closed the hijack but opened a sustained-DoS
   regression: a single authenticated account trickling ~1 wrong guess every
   couple seconds would burn every pairing, including ones created *after*
   the trickle started, within ~9s of their birth, indefinitely (re-init
   just yields a fresh code the ongoing stream immediately burns again).
   Round 2 fixes that by scoping collateral charging to a rolling
   `_BURST_WINDOW_SECONDS` window (`_recent_misses`): a pairing created
   *while a miss streak is already sustained* (>= `_BURST_MISS_THRESHOLD`
   misses within the window) is exempt from collateral strikes for its
   entire lifetime -- an attacker's ongoing trickle can never reach it, so a
   device that re-inits mid-attack always gets a pairing its owner can
   still claim. A pairing that already existed *before* any streak started
   remains fully protected by the burn (an attacker who happens to already
   know a pairing is live still can't out-guess it). See defense #2 below
   for the (small, bounded) hijack exposure this trade accepts for
   mid-attack pairings.

   An attacker who tries to dodge the burn entirely by keeping several decoy
   pairings of their own alive (so any one guess only partially "spends" any
   single *pre-streak* pairing's budget) still has to get requests past the
   rate limiter below within the 600s TTL.

2. Widened entropy (defense in depth, not the primary defense, and now also
   the *only* defense for a pairing born mid-attack per #1 above): the code
   grew from 6 to 8 numeric digits (1e6 -> 1e8 space). Binding the claim to
   a device-held secret (e.g. also requiring `poll_token`) was considered
   and rejected: the intended UX is "device shows a short code on its own
   screen; the owning user types *only that code* into their already
   logged-in session" (see static/js/devices.js) -- `poll_token` never
   reaches the human, so requiring it at claim time would break the
   legitimate flow this task is required to preserve. Lengthening the same
   kind of code keeps that flow identical while cutting brute-force odds by
   100x on top of defense #1.

   Worst case for a mid-attack pairing (no burn protection, defense #3's
   claim_rate_limiter is the only cap): 20 claims/30s per (ip, user_id) ->
   at most ~400 guesses land within any one pairing's 600s TTL even if an
   attacker saturates the limiter the whole time -> hijack probability
   <= 400 / 1e8 = 4e-6 per pairing. Negligible, and a deliberate, documented
   trade against the round-1 DoS (see task-5-report.md for the full
   before/after numbers). A pre-streak pairing keeps the much tighter
   ~6e-8-class bound defense #1 provides.

   MUST-VERIFY-BEFORE-DEPLOY: the ESP32/RPi/web-client firmware/UI that
   renders this code on-device is in separate submodules not checked out in
   this worktree -- nobody has confirmed it displays/accepts all 8 digits
   rather than truncating to 6 (e.g. a `%06d`-style format string). Verify
   before shipping this change.

3. Rate limiting (`_RateLimiter`, `claim_rate_limiter` / `init_rate_limiter`):
   a coarse in-process sliding-window limiter on both pair/claim (keyed by
   IP + user_id, since claim requires login) and pair/init (keyed by IP).
   This is process-local state, same lifecycle/caveat as the rest of this
   module and as `app.services.livehost.registry` / `_job_owners`: with more
   than one uvicorn worker or replica, each process keeps its own counters,
   so an attacker who can land requests on N processes effectively gets N x
   the budget. Fine for this deployment (single worker); a multi-worker
   rollout would need to move this to a shared store (e.g. Redis) to keep
   the guarantee.

   Separately (pre-existing infra concern, not fixed here): `_client_ip` in
   devices.py reads `request.client.host` only, with no X-Forwarded-For
   handling. Behind the production reverse proxy that collapses every
   client onto one apparent IP, so `init_rate_limiter` (IP-only) becomes an
   effectively global cap rather than a per-client one. Not attacker-
   controlled (no header to forge into a bypass), just coarser than
   intended -- flagged for whoever wires up real client-IP extraction.
-----------------------------------------------------------------------------
"""

from __future__ import annotations

import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass

_TTL_SECONDS = 600.0

_CODE_DIGITS = 8  # widened from 6 -- see module docstring, defense #2
_MAX_CLAIM_ATTEMPTS = 5  # failed claims a single pending pairing tolerates before burning

# Round-2 DoS fix: a pairing is only collateral-chargeable if it existed
# before the current miss streak became "sustained." See module docstring,
# defense #1.
_BURST_WINDOW_SECONDS = 15.0
_BURST_MISS_THRESHOLD = _MAX_CLAIM_ATTEMPTS + 1  # 6: same count that would burn one
                                                   # continuously-existing pairing


@dataclass
class PendingPairing:
    code: str
    poll_token: str
    serial: str
    expires_at: float
    created_at: float
    claimed: bool = False
    device_id: str | None = None
    token: str | None = None
    attempts: int = 0


class PendingPairingRegistry:
    def __init__(self) -> None:
        self._by_code: dict[str, PendingPairing] = {}
        self._by_poll_token: dict[str, PendingPairing] = {}
        self._recent_misses: deque[float] = deque()

    def create(self, serial: str) -> PendingPairing:
        self._sweep_expired()
        code = f"{secrets.randbelow(10 ** _CODE_DIGITS):0{_CODE_DIGITS}d}"
        poll_token = secrets.token_urlsafe(24)
        now = time.monotonic()
        entry = PendingPairing(
            code=code, poll_token=poll_token, serial=serial,
            expires_at=now + _TTL_SECONDS, created_at=now,
        )
        self._by_code[code] = entry
        self._by_poll_token[poll_token] = entry
        return entry

    def get_by_code(self, code: str) -> PendingPairing | None:
        self._sweep_expired()
        return self._by_code.get(code)

    def get_by_poll_token(self, poll_token: str) -> PendingPairing | None:
        self._sweep_expired()
        return self._by_poll_token.get(poll_token)

    def record_failed_attempt(self) -> None:
        """A pair/claim was made with a code that matched no live pairing.
        Charge the strike against every pairing that already existed
        *before the current miss streak became sustained* (see module
        docstring, defense #1) and burn any that crosses the limit --
        deleting it from both lookup tables so even its real code stops
        working. A pairing created *during* an already-sustained streak is
        exempt for its whole lifetime -- that's what stops a low-cost
        trickle from indefinitely holding the pairing feature down (round-1
        regression); its only protection is then entropy + the claim rate
        limiter (defenses #2/#3)."""
        self._sweep_expired()
        now = time.monotonic()
        self._recent_misses.append(now)
        while self._recent_misses and now - self._recent_misses[0] > _BURST_WINDOW_SECONDS:
            self._recent_misses.popleft()
        streak_start = (
            self._recent_misses[0]
            if len(self._recent_misses) >= _BURST_MISS_THRESHOLD
            else None
        )
        for code, entry in list(self._by_code.items()):
            if streak_start is not None and entry.created_at > streak_start:
                continue  # born mid-streak -- exempt, see docstring above
            entry.attempts += 1
            if entry.attempts > _MAX_CLAIM_ATTEMPTS:
                self._burn(code, entry)

    def _burn(self, code: str, entry: PendingPairing) -> None:
        self._by_code.pop(code, None)
        self._by_poll_token.pop(entry.poll_token, None)

    def mark_claimed(self, code: str, device_id: str, token: str) -> None:
        entry = self._by_code.pop(code, None)
        if entry is not None:
            entry.claimed = True
            entry.device_id = device_id
            entry.token = token

    def _sweep_expired(self) -> None:
        now = time.monotonic()
        for code, entry in list(self._by_code.items()):
            if entry.expires_at < now:
                self._by_code.pop(code, None)
        for token, entry in list(self._by_poll_token.items()):
            if entry.expires_at < now:
                self._by_poll_token.pop(token, None)

    def reset(self) -> None:
        """Test-only: drop all pending pairings so hardening tests that
        deliberately burn codes / trigger bursts don't leak state into
        other tests sharing this process-global singleton."""
        self._by_code.clear()
        self._by_poll_token.clear()
        self._recent_misses.clear()


class _RateLimiter:
    """Coarse in-process sliding-window limiter -- see module docstring,
    defense #3, for its scope and multi-worker caveat."""

    def __init__(self, max_events: int, window_seconds: float) -> None:
        self._max_events = max_events
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > self._window:
            hits.popleft()
        if len(hits) >= self._max_events:
            return False
        hits.append(now)
        return True

    def reset(self) -> None:
        """Test-only: clear all tracked keys."""
        self._hits.clear()


pending_pairings = PendingPairingRegistry()

# pair/claim requires login, so key on IP + user_id; pair/init is anonymous
# (device has no session yet), so key on IP alone. Thresholds are generous
# for legitimate use (a real user fat-fingering a code, or a device retrying
# init after a flaky connection) but crush an automated sweep: 1e8 codes at
# 20 requests/30s would take ~4.6 years to exhaust even before defense #1
# burns the target pairing after 5 wrong guesses.
claim_rate_limiter = _RateLimiter(max_events=20, window_seconds=30.0)
init_rate_limiter = _RateLimiter(max_events=30, window_seconds=30.0)
