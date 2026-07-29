from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ApiResponse(BaseModel):
    success: bool = True
    data: Any = None
    error: str | None = None


class StreamEvent(BaseModel):
    event_type: str = Field(..., description="Event category")
    session_id: str | None = None
    job_id: str | None = None
    sequence: int = 0
    timestamp: datetime = Field(default_factory=_utcnow)
    payload: dict[str, Any] = Field(default_factory=dict)


# Terminal events close their channel so subscribers (SSE) can stop cleanly.
TERMINAL_EVENT_TYPES = frozenset({"done"})


class CloneRequest(BaseModel):
    """Shared by /v1/mcp/servers/{name}/clone, /v1/profiles/{name}/clone, and
    /v1/tts/profiles/{name}/clone -- all three routes defined this identically
    before being deduped here."""

    new_name: str
