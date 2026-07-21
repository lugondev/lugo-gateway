from pydantic import BaseModel, Field


class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1)
    engine: str = "omnivoice"
    # Registry row selector: multiple rows can share an engine (e.g. three
    # openai_tts rows pointing at different service base_urls), so the engine
    # alone is ambiguous. Empty = let the provider fall back to its first
    # enabled row (legacy behavior for engines with a single row).
    model_id: str = ""
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
    duration_seconds: float | None = None  # length of the produced audio
    process_seconds: float | None = None  # wall-clock time spent synthesizing it
    job_id: str | None = None
    text: str | None = None
