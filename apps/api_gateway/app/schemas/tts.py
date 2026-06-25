from pydantic import BaseModel, Field


class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1)
    engine: str = "omnivoice"
    language: str | None = None
    speed: float | None = None
    instruct: str | None = None
    ref_audio_path: str | None = None
    ref_text: str | None = None
    voice: str | None = None  # VieNeu preset voice id


class TTSResult(BaseModel):
    engine: str
    sample_rate: int
    audio_url: str | None = None
    duration_seconds: float | None = None
    job_id: str | None = None
    text: str | None = None
    mock: bool = False
