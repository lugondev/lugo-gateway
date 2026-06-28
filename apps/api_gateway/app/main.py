from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes.agents_docs import router as agents_docs_router
from app.api.routes.conversation import router as conversation_router
from app.api.routes.events import router as events_router
from app.api.routes.health import router as health_router
from app.api.routes.stt import router as stt_router
from app.api.routes.system import router as system_router
from app.api.routes.tts import router as tts_router
from app.api.routes.ui import router as ui_router
from app.core.errors import AppError
from app.core.logging import setup_logging
from app.core.settings import settings
from app.services.artifacts import artifact_store

setup_logging(settings.log_level)

app = FastAPI(title=settings.app_name)

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
app.include_router(ui_router)
app.include_router(agents_docs_router)

app.mount("/static", StaticFiles(directory="apps/api_gateway/app/static"), name="static")
# Serve generated audio artifacts (foundation; swap for object storage later).
app.mount("/artifacts", StaticFiles(directory=str(artifact_store.base_dir)), name="artifacts")


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": settings.app_name, "env": settings.app_env}
