import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes.agents_docs import router as agents_docs_router
from app.api.routes.conversation import router as conversation_router
from app.api.routes.events import router as events_router
from app.api.routes.health import router as health_router
from app.api.routes.mcp import router as mcp_router
from app.api.routes.memories import router as memories_router
from app.api.routes.profiles import router as profiles_router
from app.api.routes.recommend import router as recommend_router
from app.api.routes.sessions import router as sessions_router
from app.api.routes.stt import router as stt_router
from app.api.routes.system import router as system_router
from app.api.routes.tts import router as tts_router
from app.api.routes.ui import router as ui_router
from app.core.errors import AppError
from app.core.logging import setup_logging
from app.core.settings import settings
from app.services.artifacts import artifact_store

setup_logging(settings.log_level)

logger = logging.getLogger(__name__)


async def _warm_default_engines() -> None:
    """Load the STT/TTS engines that new conversations use by default, at process
    boot instead of waiting for the first WebSocket connect. On a genuinely cold
    start (server and device booting together) this gives the model a head start
    while the device is still connecting, instead of racing the client's first
    utterance against a cold model load (see app.services.warmup)."""
    from app.core.errors import AppError
    from app.services.stt.service import stt_service
    from app.services.tts.service import tts_service
    from app.services.warmup import warm_providers

    try:
        stt_provider = stt_service.get_provider(settings.conversation_stt_engine)
        tts_provider = tts_service.get_provider(settings.conversation_tts_engine)
    except AppError as exc:
        logger.warning("default engine warm-up skipped: %s", exc)
        return
    await warm_providers(stt_provider, tts_provider)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.services.db.engine import init_db

    await init_db()
    asyncio.create_task(_warm_default_engines())
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(AppError)
async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "error": str(exc)},
    )


app.include_router(health_router)
app.include_router(stt_router)
app.include_router(tts_router)
app.include_router(events_router)
app.include_router(conversation_router)
app.include_router(system_router)
app.include_router(recommend_router)
app.include_router(ui_router)
app.include_router(agents_docs_router)
app.include_router(profiles_router)
app.include_router(mcp_router)
app.include_router(sessions_router)
app.include_router(memories_router)

app.mount("/static", StaticFiles(directory="apps/api_gateway/app/static"), name="static")
# Serve generated audio artifacts (foundation; swap for object storage later).
app.mount("/artifacts", StaticFiles(directory=str(artifact_store.base_dir)), name="artifacts")


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": settings.app_name, "env": settings.app_env}
