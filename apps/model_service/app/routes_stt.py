"""OpenAI-compatible transcription endpoint over a gateway STT provider."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.services.stt.base import STTProvider
from model_service.app.auth import make_auth_dependency
from model_service.app.config import ServiceConfig


def build_stt_router(config: ServiceConfig, provider: STTProvider) -> APIRouter:
    router = APIRouter(prefix="/v1", dependencies=[Depends(make_auth_dependency(config.api_token))])

    @router.get("/models")
    async def list_models() -> dict:
        return {"object": "list", "data": [{"id": config.engine, "object": "model", "owned_by": "local"}]}

    @router.post("/audio/transcriptions")
    async def create_transcription(
        file: UploadFile = File(...),
        model: str = Form(default=""),
        language: str = Form(default=""),
        response_format: str = Form(default="json"),
    ) -> dict:
        audio = await file.read()
        if not audio:
            raise HTTPException(status_code=400, detail="uploaded file is empty")

        # `model` is a model name (the gateway sends its registry entry's
        # model_id), not the engine name -- forward it; the provider takes a
        # per-call model and falls back to its configured default on None.
        result = await provider.transcribe_bytes(audio, language or None, model or None)
        return {"text": result.text}

    return router
