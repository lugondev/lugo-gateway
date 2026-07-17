"""Standalone STT/TTS service: one engine per container, chosen by env.

Wraps the gateway's existing providers in an OpenAI-compatible HTTP surface so
the gateway can consume them as a service (a Model Registry base_url) instead
of loading the model in-process.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import EngineNotFoundError, ProviderError
from model_service.app.config import ConfigError, ServiceConfig, load_config

logger = logging.getLogger(__name__)


def _resolve_provider(config: ServiceConfig):
    """Fetch the configured provider, failing fast on an unknown engine."""
    if config.kind == "stt":
        from app.services.stt.service import stt_service

        return stt_service.get_provider(config.engine)

    from app.services.tts.base import RenderingTTSProvider
    from app.services.tts.service import tts_service

    provider = tts_service.get_provider(config.engine)
    if not isinstance(provider, RenderingTTSProvider):
        # Only WAV-rendering engines can serve raw bytes on the response. The
        # odd one out is edge_tts, which is a cloud service anyway.
        raise ConfigError(
            f"TTS engine '{config.engine}' is not a RenderingTTSProvider and cannot be served"
        )
    return provider


def create_app(config: ServiceConfig | None = None, provider=None) -> FastAPI:
    config = config or load_config()
    if provider is None:
        provider = _resolve_provider(config)

    app = FastAPI(title=f"model-service ({config.kind}:{config.engine})")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "kind": config.kind, "engine": config.engine}

    if config.kind == "stt":
        from model_service.app.routes_stt import build_stt_router

        app.include_router(build_stt_router(config, provider))
    else:
        from model_service.app.routes_tts import build_tts_router

        app.include_router(build_tts_router(config, provider))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request, exc: StarletteHTTPException):
        # Registering on Starlette's HTTPException (rather than FastAPI's
        # subclass) also catches routing-level 404/405s, which Starlette
        # raises directly and would otherwise leak FastAPI's stock
        # {"detail": ...} shape instead of the OpenAI envelope.
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"message": exc.detail, "type": "invalid_request_error"}},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request, exc: RequestValidationError):
        # RequestValidationError (pydantic/FastAPI body & form validation) is
        # not an HTTPException subclass, so it bypasses the handler above and
        # would otherwise leak FastAPI's stock {"detail": [...]} shape -- the
        # one case where an OpenAI-compatible client couldn't parse our error.
        errors = exc.errors()
        if errors:
            first = errors[0]
            field = ".".join(str(p) for p in first["loc"] if p != "body")
            message = f"{field}: {first['msg']}" if field else first["msg"]
        else:
            # Every real FastAPI/pydantic validation failure produces at
            # least one error item, but exc.errors()[0] would IndexError
            # here if a future validator ever raised
            # RequestValidationError([]) -- turning a clean 422 into a bare
            # 500 from inside the error handler itself.
            message = "invalid request"
        return JSONResponse(
            status_code=422, content={"error": {"message": message, "type": "invalid_request_error"}}
        )

    @app.exception_handler(EngineNotFoundError)
    async def _engine_error(_request, exc: EngineNotFoundError):
        return JSONResponse(
            status_code=400, content={"error": {"message": str(exc), "type": "invalid_request_error"}}
        )

    @app.exception_handler(ProviderError)
    async def _provider_error(_request, exc: ProviderError):
        # The engine itself failed (OOM, model missing): the request was fine,
        # so this is 502 rather than 400 -- and the caller owns the retry.
        logger.exception("provider failed")
        return JSONResponse(
            status_code=502, content={"error": {"message": str(exc), "type": "provider_error"}}
        )

    @app.exception_handler(Exception)
    async def _unhandled_error(_request, exc: Exception):
        # Catch-all so an unexpected bug in our own code never leaks a plain
        # 500 text/plain response -- but keep it distinct from provider_error
        # (502): this is *our* bug, not the engine failing.
        logger.exception("unhandled error in model-service")
        return JSONResponse(
            status_code=500,
            content={"error": {"message": "internal server error", "type": "internal_error"}},
        )

    return app
