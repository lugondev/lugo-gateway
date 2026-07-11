"""In-memory pending-device-pairing registry -- same pattern as
app.services.livehost.registry.livehost_registry (a process-global dict is
fine here: entries are short-lived (~10 min TTL) and losing them on restart
just means the device retries pair/init)."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

_TTL_SECONDS = 600.0


@dataclass
class PendingPairing:
    code: str
    poll_token: str
    serial: str
    expires_at: float
    claimed: bool = False
    device_id: str | None = None
    token: str | None = None


class PendingPairingRegistry:
    def __init__(self) -> None:
        self._by_code: dict[str, PendingPairing] = {}
        self._by_poll_token: dict[str, PendingPairing] = {}

    def create(self, serial: str) -> PendingPairing:
        self._sweep_expired()
        code = f"{secrets.randbelow(1_000_000):06d}"
        poll_token = secrets.token_urlsafe(24)
        entry = PendingPairing(
            code=code, poll_token=poll_token, serial=serial,
            expires_at=time.monotonic() + _TTL_SECONDS,
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


pending_pairings = PendingPairingRegistry()
