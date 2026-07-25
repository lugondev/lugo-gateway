from pydantic import BaseModel, Field


class STTRequest(BaseModel):
    engine: str = Field(
        default="vosk",
        pattern="^(vosk|whisper|whisper_local|whisper_mlx|qwen3_asr|qwen3_asr_gguf|whisper_service|eventlab|qwen3_asr_or|whisper_or|http_stt|qwencloud)$",
    )
    language: str | None = None
    model: str | None = None


class STTResult(BaseModel):
    engine: str
    text: str
    is_final: bool = True
    confidence: float | None = None
