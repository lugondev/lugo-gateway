from pydantic import BaseModel, Field


class STTRequest(BaseModel):
    engine: str = Field(
        default="vosk",
        pattern="^(vosk|whisper|whisper_local|whisper_mlx|qwen_omni|sensevoice|whisper_gemma|whisper_service|eventlab)$",
    )
    language: str | None = None


class STTResult(BaseModel):
    engine: str
    text: str
    is_final: bool = True
    confidence: float | None = None
