"""Standalone STT/TTS service: one engine per container, chosen by env.

Wraps the gateway's existing providers in an OpenAI-compatible HTTP surface so
the gateway can consume them as a service (a Model Registry base_url) instead
of loading the model in-process.
"""

from __future__ import annotations

import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import EngineNotFoundError, ProviderError
from model_service.app.config import ConfigError, ServiceConfig, load_config

logger = logging.getLogger(__name__)

_TMP_REF_FILENAME = re.compile(r"^[0-9a-f]{32}\.wav\Z")

# The four local TTS engines run as separate native processes sharing the same
# repo-root artifacts directory (not one process per container), so a bare-hex
# .wav can be a sibling process's in-flight temp file, not just this process's
# own leftovers -- "one process, one file, one call" is not a safe assumption
# here. Anything younger than this is left alone; anything older cannot belong
# to a live request in any process, since routes_tts.py holds its own file for
# only the duration of a single call. A leak that's fresher than the threshold
# just waits for the next restart to be collected -- restarts are rare and a
# leaked file is cheap, whereas deleting a live request's reference audio out
# from under an open read breaks that request's synthesis.
_STALE_AGE_SECONDS = 3600


def sweep_stale_ref_audio(base_dir: Path) -> int:
    """Delete temp reference clips left behind by a previous run.

    routes_tts.py writes `<uuid4 hex>.wav` into the artifacts dir (it has to
    live there to pass TTSRequest's ref_audio_path containment check) and
    unlinks it in a finally. A crash in between used to be mopped up by the
    gateway's artifact janitor, which no longer exists -- synthesized audio is
    never persisted, so there was nothing else left for it to prune. At
    startup, a bare-hex `.wav` older than `_STALE_AGE_SECONDS` cannot belong to
    any live request, in this process or a sibling one sharing the same
    directory -- see the module comment above. `ref_*.wav` never matches this
    pattern and is never touched.
    """
    if not base_dir.is_dir():
        return 0
    try:
        candidates = list(base_dir.iterdir())
    except OSError:
        # The directory exists (is_dir() above didn't raise -- stat-ing a path
        # doesn't require list permission on it) but can't be listed, e.g. a
        # permissions error. Degrade to "skip the sweep" rather than crashing
        # app startup over a cleanup nicety.
        return 0
    removed = 0
    now = time.time()
    for path in candidates:
        if not _TMP_REF_FILENAME.match(path.name):
            continue
        try:
            if now - path.stat().st_mtime < _STALE_AGE_SECONDS:
                continue
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


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


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Real-boot-only: uvicorn drives the ASGI lifespan protocol (`uvicorn
    # model_service.app.main:create_app --factory`, see
    # infra/docker/Dockerfile.model_service), so this runs once per process
    # start. Deliberately NOT called inline in create_app() -- unit tests
    # construct the app via `TestClient(create_app(...))` without entering it
    # as a context manager, so lifespan never fires there, and this sweep
    # never touches a developer's real repo-root artifacts/ dir during the
    # unit suite (see test_routes_tts.py/test_routes_stt.py, which don't use
    # `with TestClient(...) as client:`).
    from app.services.artifacts import artifact_store

    swept = sweep_stale_ref_audio(artifact_store.base_dir)
    if swept:
        logger.info("swept %d stale temp reference clip(s) from artifacts dir", swept)
    yield


def create_app(config: ServiceConfig | None = None, provider=None) -> FastAPI:
    config = config or load_config()
    if provider is None:
        provider = _resolve_provider(config)

    app = FastAPI(title=f"model-service ({config.kind}:{config.engine})", lifespan=_lifespan)

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
