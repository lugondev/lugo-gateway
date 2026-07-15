from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel

from app.services.install_manager import install_manager
from app.services.recommend.service import recommend_all

router = APIRouter(prefix="/v1", tags=["recommend"])


class InstallRequest(BaseModel):
    package: str


@router.get("/models/recommend")
async def models_recommend() -> dict:
    """Config-aware ranking of every STT/TTS/LLM/VAD model for THIS host.

    Read-only: each item carries a fit score, runnable status, reason, and the
    download/install action — the UI filters (recommended-only) and groups (by chip).
    """
    return {"success": True, "data": await recommend_all()}


@router.post("/models/install")
async def models_install(payload: InstallRequest, background: BackgroundTasks) -> dict:
    """Pip-install an optional engine package (gated by ALLOW_RUNTIME_INSTALL,
    allowlist-only). Validates synchronously, then installs in the background."""
    install_manager.validate(payload.package)  # raises 403 (disabled) / 400 (not allowed)
    background.add_task(install_manager.install, payload.package)
    return {"success": True, "data": {"package": payload.package, "state": "installing"}}
