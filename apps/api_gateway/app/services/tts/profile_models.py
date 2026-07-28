from typing import Literal

from pydantic import BaseModel, field_validator

from app.services.artifacts import artifact_store


class TtsProfile(BaseModel):
    name: str
    owner_id: str | None = None
    engine: str = ""
    model_id: str = ""         # registry row id within the engine (see TTSRequest.model_id)
    voice_mode: Literal["preset", "clone"] = "preset"
    voice: str = ""            # preset mode: voice id from GET /v1/tts/voices?engine=
    ref_audio_path: str = ""   # clone mode
    ref_text: str = ""         # clone mode: transcript of the reference audio
    instruct: str = ""         # style/emotion instruction (engine-dependent, e.g. omnivoice)
    speed: float | None = None
    language: str | None = None

    @field_validator("ref_audio_path")
    @classmethod
    def _ref_audio_path_must_stay_in_artifacts_dir(cls, v: str) -> str:
        """Same containment rule as TTSRequest.ref_audio_path (schemas/tts.py,
        2026-07-28-critical-authz-fixes task 5) -- but enforced here too so a
        bad path is rejected at SAVE time (POST/PUT /v1/tts/profiles, a clear
        422) instead of at synthesis time. Before this, an out-of-bounds
        value saved via the profile route only failed later when a session
        built a TTSRequest from it -- and did so with an uncaught
        ValidationError that unwound the whole conversation turn instead of
        degrading to `tts_error` (see session.py task-6-fixes-round-1 I2).
        Empty string is this model's "not set" sentinel, not a path."""
        if not v:
            return v
        if not artifact_store.contains(v):
            raise ValueError("ref_audio_path must be inside the artifacts directory")
        return v
