from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, field_validator

from app.api.routes.profiles import _visible
from app.core.actor import current_user_id
from app.services.memory.store import memory_store
from app.services.profiles.store import profile_store

router = APIRouter(prefix="/v1/profiles/{name}/memories", tags=["memories"])


def _require_visible(name: str, request: Request) -> str:
    """The caller may touch a profile's memories iff they can see the profile.
    The only bucket they can reach is their own (every store call is scoped to
    current_user_id), so read and write share the same gate. Returns the
    caller's user_id (normalized to '' when there is no logged-in user, e.g.
    dev-mode auth-off). Always 404 -- mirrors profiles.py so probing cannot
    enumerate other users' profile names."""
    profile = profile_store.get(name)
    user_id = current_user_id(request)
    if profile is None or not _visible(profile, user_id):
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    return user_id or ""


class MemoryRequest(BaseModel):
    content: str

    @field_validator("content")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("content must not be blank")
        return v


@router.get("")
async def list_memories(name: str, request: Request) -> dict:
    user_id = _require_visible(name, request)
    return {"success": True, "data": await memory_store.list(name, user_id=user_id)}


@router.post("")
async def add_memory(name: str, payload: MemoryRequest, request: Request) -> dict:
    user_id = _require_visible(name, request)
    row = await memory_store.add(name, payload.content, user_id=user_id)
    return {"success": True, "data": row}


@router.put("/{memory_id}")
async def update_memory(name: str, memory_id: str, payload: MemoryRequest, request: Request) -> dict:
    user_id = _require_visible(name, request)
    row = await memory_store.update(memory_id, payload.content, profile_id=name, user_id=user_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Memory '{memory_id}' not found")
    return {"success": True, "data": row}


@router.delete("/{memory_id}")
async def delete_memory(name: str, memory_id: str, request: Request) -> dict:
    user_id = _require_visible(name, request)
    if not await memory_store.delete(memory_id, profile_id=name, user_id=user_id):
        raise HTTPException(status_code=404, detail=f"Memory '{memory_id}' not found")
    return {"success": True, "data": {"id": memory_id, "deleted": True}}


@router.delete("")
async def delete_all_memories(name: str, request: Request) -> dict:
    user_id = _require_visible(name, request)
    count = await memory_store.delete_all(name, user_id=user_id)
    return {"success": True, "data": {"deleted": count}}
