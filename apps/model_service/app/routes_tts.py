"""OpenAI-compatible speech endpoint over a gateway TTS provider.

`response_format` is accepted but ignored: only RenderingTTSProvider engines
are servable and they all produce WAV.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.schemas.tts import TTSRequest
from app.services.tts.base import RenderingTTSProvider
from model_service.app.auth import make_auth_dependency
from model_service.app.config import ServiceConfig


class SpeechRequest(BaseModel):
    input: str = Field(..., min_length=1)
    model: str = ""
    voice: str | None = None
    speed: float | None = None
    language: str | None = None
    response_format: str = "wav"


def build_tts_router(config: ServiceConfig, provider: RenderingTTSProvider) -> APIRouter:
    router = APIRouter(prefix="/v1", dependencies=[Depends(make_auth_dependency(config.api_token))])

    @router.get("/models")
    async def list_models() -> dict:
        return {"object": "list", "data": [{"id": config.engine, "object": "model", "owned_by": "local"}]}

    @router.post("/audio/speech")
    async def create_speech(payload: SpeechRequest) -> Response:
        wav = await provider.render_wav(
            TTSRequest(
                text=payload.input,
                engine=config.engine,
                voice=payload.voice,
                speed=payload.speed,
                language=payload.language,
            )
        )
        return Response(content=wav, media_type="audio/wav")

    return router
