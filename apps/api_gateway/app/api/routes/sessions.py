from fastapi import APIRouter, HTTPException, Request

from app.core.actor import scope_user_id
from app.schemas.sessions import BulkDeleteRequest
from app.services.history.store import session_store

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])


# None for admins, or when auth is fully disabled (dev mode -- see
# app.core.actor.current_role); the caller's own id otherwise, so a regular
# user only ever sees their own sessions. Imported under the private name this
# module has always exported (api/routes/conversation.py imports it from here,
# and tests monkeypatch it per-route) -- the body itself now lives in
# core/actor.py, shared with livehost.py's identical copy.
_scope_user_id = scope_user_id


@router.get("")
async def list_sessions(
    request: Request,
    profile: str | None = None,
    limit: int = 20,
    offset: int = 0,
    source: str | None = None,
    client_id: str | None = None,
) -> dict:
    """`source`/`client_id` filter by which client held the conversation
    ("device" vs "web"), so History can separate what was said to the speaker from
    what was typed in the browser. Rows written before provenance existed carry
    "" and match neither filter."""
    rows = await session_store.list(
        profile_id=profile, user_id=_scope_user_id(request), limit=limit, offset=offset,
        source=source, client_id=client_id,
    )
    return {"success": True, "data": rows}


@router.post("/bulk_delete")
async def bulk_delete_sessions(payload: BulkDeleteRequest, request: Request) -> dict:
    """Delete the listed sessions. Missing IDs are skipped, not errors. A
    non-admin's request is first filtered down to only the ids they own."""
    ids = payload.ids
    scope = _scope_user_id(request)
    if scope is not None:
        owned = {r["id"] for r in await session_store.list(user_id=scope, limit=10_000)}
        ids = [i for i in ids if i in owned]
    deleted = await session_store.delete_many(ids)
    return {"success": True, "data": {"deleted": deleted}}


@router.delete("")
async def clear_sessions(request: Request, profile: str | None = None, only_empty: bool = False) -> dict:
    """Clear sessions in scope. Non-admins may only clear their own; enforced by
    deleting via bulk_delete-style id filtering rather than the unfiltered
    profile-only clear() method, which stays admin-only."""
    scope = _scope_user_id(request)
    if scope is None:
        deleted = await session_store.clear(profile_id=profile, only_empty=only_empty)
        return {"success": True, "data": {"deleted": deleted}}
    owned = await session_store.list(profile_id=profile, user_id=scope, limit=10_000)
    ids = [r["id"] for r in owned if not only_empty or r["message_count"] == 0]
    deleted = await session_store.delete_many(ids)
    return {"success": True, "data": {"deleted": deleted}}


@router.get("/{session_id}")
async def get_session(session_id: str, request: Request) -> dict:
    sess = await session_store.get(session_id)
    scope = _scope_user_id(request)
    if not sess or (scope is not None and sess.get("user_id") != scope):
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    sess["messages"] = await session_store.get_messages(session_id)
    return {"success": True, "data": sess}


@router.delete("/{session_id}")
async def delete_session(session_id: str, request: Request) -> dict:
    sess = await session_store.get(session_id)
    scope = _scope_user_id(request)
    if not sess or (scope is not None and sess.get("user_id") != scope):
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    await session_store.delete(session_id)
    return {"success": True, "data": {"id": session_id, "deleted": True}}
