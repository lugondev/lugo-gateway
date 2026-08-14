from fastapi import APIRouter, HTTPException, Request

from app.api.routes.profiles import _visible
from app.core.actor import current_user_id
from app.schemas.memories import MemoryRequest
from app.services.memory.store import memory_store
from app.services.profile_visibility import profile_usable
from app.services.profiles.store import profile_store

router = APIRouter(prefix="/v1/profiles/{name}/memories", tags=["memories"])


def _require_visible(name: str, request: Request) -> str:
    """The caller may READ a profile's memories iff they can see the profile
    -- includes a shared template, same as GET /v1/profiles listing it to
    everyone. The only bucket they can reach is their own (every store call
    is scoped to current_user_id), so a template's bucket is harmless to
    expose: it is always empty (writes are gated separately, see
    _require_usable). Returns the caller's user_id (normalized to '' when
    there is no logged-in user, e.g. dev-mode auth-off). Always 404 for the
    private/missing case -- mirrors profiles.py so probing cannot enumerate
    other users' profile names."""
    profile = profile_store.get(name)
    user_id = current_user_id(request)
    if profile is None or not _visible(profile, user_id):
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    return user_id or ""


def _require_usable(name: str, request: Request) -> str:
    """The caller may WRITE a profile's memories iff they could actually RUN
    it (usable(), not merely visible()). A shared template is visible to
    everyone but runnable by nobody -- letting a write land there would
    create rows under a name that profile can never run under (and a clone
    gets a different name), permanently orphaning them. Same 404-collapsing
    shape as _require_visible, so this creates no new enumeration oracle: a
    shared row's existence is already public via GET /v1/profiles, and every
    other case (missing, someone else's private row) reads identically to
    before."""
    profile = profile_store.get(name)
    user_id = current_user_id(request)
    if profile is None or not profile_usable(profile, user_id):
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    return user_id or ""


@router.get("")
async def list_memories(name: str, request: Request) -> dict:
    user_id = _require_visible(name, request)
    return {"success": True, "data": await memory_store.list(name, user_id=user_id)}


@router.post("")
async def add_memory(name: str, payload: MemoryRequest, request: Request) -> dict:
    user_id = _require_usable(name, request)
    row = await memory_store.add(name, payload.content, user_id=user_id)
    return {"success": True, "data": row}


@router.put("/{memory_id}")
async def update_memory(name: str, memory_id: str, payload: MemoryRequest, request: Request) -> dict:
    user_id = _require_usable(name, request)
    row = await memory_store.update(memory_id, payload.content, profile_id=name, user_id=user_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Memory '{memory_id}' not found")
    return {"success": True, "data": row}


@router.delete("/{memory_id}")
async def delete_memory(name: str, memory_id: str, request: Request) -> dict:
    user_id = _require_usable(name, request)
    if not await memory_store.delete(memory_id, profile_id=name, user_id=user_id):
        raise HTTPException(status_code=404, detail=f"Memory '{memory_id}' not found")
    return {"success": True, "data": {"id": memory_id, "deleted": True}}


@router.delete("")
async def delete_all_memories(name: str, request: Request) -> dict:
    user_id = _require_usable(name, request)
    count = await memory_store.delete_all(name, user_id=user_id)
    return {"success": True, "data": {"deleted": count}}
