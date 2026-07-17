"""OpenAI-compatible transcription endpoint over a gateway STT provider."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.core.errors import EngineNotFoundError, ProviderError
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
        try:
            result = await provider.transcribe_bytes(audio, language or None, model or None)
        except (EngineNotFoundError, HTTPException):
            # Already map cleanly via their own handlers -- don't relabel them.
            raise
        except Exception as exc:  # noqa: BLE001 - surface as a clean provider error
            # Real STT providers raise bare RuntimeError (unreadable audio,
            # model OOM, etc); mirror RenderingTTSProvider.render_wav's seam so
            # the OpenAI error envelope covers the whole failure surface.
            raise ProviderError(f"{provider.name} transcription failed: {exc}") from exc
        return {"text": result.text}

    return router
