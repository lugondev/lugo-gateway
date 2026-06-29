from fastapi import APIRouter

from app.services.recommend.service import recommend_all

router = APIRouter(prefix="/v1", tags=["recommend"])


@router.get("/models/recommend")
async def models_recommend() -> dict:
    """Config-aware ranking of every STT/TTS/LLM/VAD model for THIS host.

    Read-only: each item carries a fit score, runnable status, reason, and the
    download/install action — the UI filters (recommended-only) and groups (by chip).
    """
    return {"success": True, "data": recommend_all()}
