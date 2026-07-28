from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

# "not_ready" is a local engine still running warm() -- distinct from
# "unavailable" (misconfigured, or a remote host that isn't answering) because
# only the latter is worth refusing a session over.
EngineStatus = Literal["ok", "not_ready", "unavailable"]


class EngineHealth(BaseModel):
    engine: str
    status: EngineStatus
    detail: str = ""

    @property
    def blocks_session(self) -> bool:
        return self.status == "unavailable"


class ProfileHealth(BaseModel):
    profile: str
    stt: EngineHealth
    tts: EngineHealth
