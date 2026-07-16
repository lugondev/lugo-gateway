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
from app.api.routes.devices import router as devices_router
from app.api.routes.events import router as events_router
from app.api.routes.health import router as health_router
from app.api.routes.livehost import router as livehost_router
from app.api.routes.lugo import router as lugo_router
from app.api.routes.mcp import router as mcp_router
from app.api.routes.memories import router as memories_router
from app.api.routes.model_registry import router as model_registry_router
from app.api.routes.profiles import router as profiles_router
from app.api.routes.recommend import router as recommend_router
from app.api.routes.sessions import router as sessions_router
from app.api.routes.stt import router as stt_router
from app.api.routes.system import router as system_router
from app.api.routes.tts import router as tts_router
from app.api.routes.tts_profiles import router as tts_profiles_router
from app.api.routes.ui import router as ui_router
from app.api.routes.users import router as users_router
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
    from app.services.stt.model_registry import apply_stt_model
    from app.services.stt.service import stt_service
    from app.services.tts.service import tts_service
    from app.services.warmup import engines_for_boot_warmup, warm_providers

    # Warm every engine any profile / TTS profile can select, not just the static
    # warmup lists — so a device connecting with any profile never pays a cold
    # model load on its first turn.
    stt_engines, tts_engines, stt_models = engines_for_boot_warmup()
    for engine, model in stt_models.items():
        try:
            apply_stt_model(engine, model)
        except AppError as exc:
            logger.warning("stt model warm-up skipped for %s/%s: %s", engine, model, exc)
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


async def _bootstrap_admin_if_needed() -> None:
    from app.services.auth.users import user_store

    if await user_store.count() > 0:
        return
    username = settings.admin_bootstrap_username or "admin"
    password = settings.admin_bootstrap_password or settings.admin_password
    if not password:
        logger.warning(
            "no admin bootstrap credentials set (ADMIN_BOOTSTRAP_PASSWORD or "
            "legacy ADMIN_PASSWORD) -- create the first admin account manually"
        )
        return
    await user_store.create(username, password, role="admin")
    logger.info("bootstrap admin account created: %s", username)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.services.db.engine import init_db

    await init_db()
    await _bootstrap_admin_if_needed()

    from app.services.db.sync_engine import init_config_tables
    from app.services.profiles.store import profile_store
    from app.services.tts.profile_store import tts_profile_store
    from app.services.system_config import system_config_store
    from app.services.mcp.server_store import mcp_server_store
    from app.services.mcp.presets import seed_default_servers

    init_config_tables()
    profile_store.list()
    tts_profile_store.list()
    system_config_store.get()
    mcp_server_store.list()
    # Seeded here (app startup), not at module-import time: import happens
    # during test collection too, before any tmp-DB fixture is in effect,
    # so seeding there would write the preset server straight into whatever
    # DB is configured at that moment -- in practice, the real data/app.db.
    seed_default_servers(mcp_server_store)

    from app.services.model_registry.seed import (
        migrate_conversation_llm_to_registry,
        migrate_omnivoice_to_registry,
        migrate_remote_stt_to_registry,
        migrate_stt_local_device_to_registry,
        migrate_stt_local_models_to_registry,
        seed_known_models,
    )

    await seed_known_models()
    await migrate_conversation_llm_to_registry()
    await migrate_remote_stt_to_registry()
    await migrate_stt_local_device_to_registry()
    await migrate_stt_local_models_to_registry()
    await migrate_omnivoice_to_registry()

    if not settings.auth_enabled and settings.app_env != "dev":
        logger.warning(
            "auth disabled: neither ADMIN_PASSWORD nor ADMIN_BOOTSTRAP_PASSWORD is set (app_env=%s)",
            settings.app_env,
        )
    # Warm engines BEFORE the app starts serving so the very first device turn is
    # instant instead of paying a cold model load (worse with connect-on-wake,
    # where the session starts the moment the user wakes). Capped so a stuck/slow
    # warm can't block startup forever (health checks); on timeout we serve cold.
    engine_defaults = system_config_store.get().engines
    if engine_defaults.warmup_on_startup:
        try:
            await asyncio.wait_for(_warm_default_engines(), timeout=engine_defaults.warmup_startup_timeout_s)
        except TimeoutError:
            logger.warning(
                "boot warm-up exceeded %ss — serving anyway; the first turn may be cold",
                engine_defaults.warmup_startup_timeout_s,
            )
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
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
    same_site="lax",
    https_only=settings.app_env != "dev",
)


@app.exception_handler(AppError)
async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "error": str(exc)},
    )


app.include_router(health_router)
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(stt_router)
app.include_router(tts_router)
app.include_router(tts_profiles_router)
app.include_router(events_router)
app.include_router(conversation_router)
app.include_router(devices_router)
app.include_router(system_router)
app.include_router(livehost_router)
app.include_router(lugo_router)
app.include_router(recommend_router)
app.include_router(ui_router)
app.include_router(agents_docs_router)
app.include_router(profiles_router)
app.include_router(mcp_router)
app.include_router(sessions_router)
app.include_router(memories_router)
app.include_router(model_registry_router)

app.mount("/static", StaticFiles(directory="apps/api_gateway/app/static"), name="static")
# Serve generated audio artifacts (foundation; swap for object storage later).
app.mount("/artifacts", StaticFiles(directory=str(artifact_store.base_dir)), name="artifacts")


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": settings.app_name, "env": settings.app_env}
