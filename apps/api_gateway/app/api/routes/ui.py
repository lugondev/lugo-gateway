from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.core.static_paths import INDEX_HTML

router = APIRouter(tags=["ui"])


@router.get("/ui")
async def ui() -> FileResponse:
    return FileResponse(INDEX_HTML)
