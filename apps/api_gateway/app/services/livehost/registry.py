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
