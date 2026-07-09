from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.history.store import session_store

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])


class BulkDeleteRequest(BaseModel):
    ids: list[str] = []


@router.get("")
async def list_sessions(profile: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    rows = await session_store.list(profile_id=profile, limit=limit, offset=offset)
    return {"success": True, "data": rows}


@router.post("/bulk_delete")
async def bulk_delete_sessions(payload: BulkDeleteRequest) -> dict:
    """Delete the listed sessions. Missing IDs are skipped, not errors."""
    deleted = await session_store.delete_many(payload.ids)
    return {"success": True, "data": {"deleted": deleted}}


@router.delete("")
async def clear_sessions(profile: str | None = None, only_empty: bool = False) -> dict:
    """Clear sessions in scope. profile filters by profile (unset = all sessions);
    only_empty restricts to sessions with no messages."""
    deleted = await session_store.clear(profile_id=profile, only_empty=only_empty)
    return {"success": True, "data": {"deleted": deleted}}


@router.get("/{session_id}")
async def get_session(session_id: str) -> dict:
    sess = await session_store.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    sess["messages"] = await session_store.get_messages(session_id)
    return {"success": True, "data": sess}


@router.delete("/{session_id}")
async def delete_session(session_id: str) -> dict:
    if not await session_store.delete(session_id):
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return {"success": True, "data": {"id": session_id, "deleted": True}}
