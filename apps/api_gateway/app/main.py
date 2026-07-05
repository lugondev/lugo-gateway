import asyncio
import logging
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.api.routes.agents_docs import router as agents_docs_router
from app.api.routes.auth import router as auth_router
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
from app.core.auth_guard import AuthGuardMiddleware
from app.core.errors import AppError
from app.core.logging import setup_logging
from app.core.settings import settings
from app.services.artifacts import artifact_store

setup_logging(settings.log_level)

logger = logging.getLogger(__name__)


async def _warm_default_engines() -> None:
    """Load the STT/TTS engines conversations actually use, at process boot instead
    of waiting for the first WebSocket connect. Covers conversation_stt_engine /
    conversation_tts_engine PLUS any extra_warmup_stt_engines/extra_warmup_tts_engines
    — a device that always pins a different engine via ?stt_engine=... (e.g. an RPi
    client configured for qwen3_asr) never touches the settings default, so it must
    be listed explicitly or this warm-up silently loads the wrong model and the
    device still pays a full cold-load on its first-ever turn each boot (see
    app.services.warmup)."""
    from app.core.errors import AppError
    from app.services.stt.service import stt_service
    from app.services.tts.service import tts_service
    from app.services.warmup import warm_providers

    stt_engines = settings.warmup_stt_engines
    tts_engines = settings.warmup_tts_engines
    providers = []
    for name in stt_engines:
        try:
            providers.append(stt_service.get_provider(name))
        except AppError as exc:
            logger.warning("stt warm-up skipped for %s: %s", name, exc)
    for name in tts_engines:
        try:
            providers.append(tts_service.get_provider(name))
        except AppError as exc:
            logger.warning("tts warm-up skipped for %s: %s", name, exc)
    if not providers:
        return

    started = time.monotonic()
    logger.info("boot warm-up starting: stt=%s tts=%s", stt_engines, tts_engines)
    await warm_providers(*providers)
    logger.info("boot warm-up finished in %.0fms", (time.monotonic() - started) * 1000)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.services.db.engine import init_db

    await init_db()
    if not settings.admin_password and settings.app_env != "dev":
        logger.warning("auth disabled: ADMIN_PASSWORD not set (app_env=%s)", settings.app_env)
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

app.add_middleware(AuthGuardMiddleware)
_session_secret = settings.session_secret or secrets.token_hex(32)
app.add_middleware(SessionMiddleware, secret_key=_session_secret, same_site="lax")


@app.exception_handler(AppError)
async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "error": str(exc)},
    )


app.include_router(health_router)
app.include_router(auth_router)
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
