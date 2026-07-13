from typing import Literal

from pydantic import BaseModel


class TtsProfile(BaseModel):
    name: str
    owner_id: str | None = None
    engine: str = ""
    voice_mode: Literal["preset", "clone"] = "preset"
    voice: str = ""            # preset mode: voice id from GET /v1/tts/voices?engine=
    ref_audio_path: str = ""   # clone mode
    ref_text: str = ""         # clone mode: transcript of the reference audio
    instruct: str = ""         # style/emotion instruction (engine-dependent, e.g. omnivoice)
    speed: float | None = None
    language: str | None = None
