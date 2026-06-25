from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(tags=["ui"])


@router.get("/ui")
async def ui() -> FileResponse:
    return FileResponse("apps/api_gateway/app/static/index.html")
