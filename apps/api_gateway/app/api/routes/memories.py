from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, field_validator

from app.api.routes.profiles import _can_write, _visible
from app.core.actor import current_role, current_user_id
from app.services.memory.store import memory_store
from app.services.profiles.store import profile_store

router = APIRouter(prefix="/v1/profiles/{name}/memories", tags=["memories"])


def _require_profile(name: str, request: Request, *, write: bool) -> None:
    """Memories are scoped by profile name, so access follows the parent
    profile's ownership: read == _visible, write == _can_write (a template's
    memories are readable by all but writable only by an admin). Always 404,
    never 403 -- mirrors profiles.py, so probing this route can't be used to
    enumerate which profile names other users own.

    Note this gates the REST surface only; the extractor writes through
    memory_store directly and is unaffected."""
    profile = profile_store.get(name)
    allowed = profile is not None and (
        _can_write(profile, current_user_id(request), current_role(request))
        if write
        else _visible(profile, current_user_id(request))
    )
    if not allowed:
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")


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
    _require_profile(name, request, write=False)
    return {"success": True, "data": await memory_store.list(name)}


@router.post("")
async def add_memory(name: str, payload: MemoryRequest, request: Request) -> dict:
    _require_profile(name, request, write=True)
    row = await memory_store.add(name, payload.content)
    return {"success": True, "data": row}


@router.put("/{memory_id}")
async def update_memory(name: str, memory_id: str, payload: MemoryRequest, request: Request) -> dict:
    _require_profile(name, request, write=True)
    row = await memory_store.update(memory_id, payload.content, profile_id=name)
    if not row:
        raise HTTPException(status_code=404, detail=f"Memory '{memory_id}' not found")
    return {"success": True, "data": row}


@router.delete("/{memory_id}")
async def delete_memory(name: str, memory_id: str, request: Request) -> dict:
    _require_profile(name, request, write=True)
    if not await memory_store.delete(memory_id, profile_id=name):
        raise HTTPException(status_code=404, detail=f"Memory '{memory_id}' not found")
    return {"success": True, "data": {"id": memory_id, "deleted": True}}


@router.delete("")
async def delete_all_memories(name: str, request: Request) -> dict:
    _require_profile(name, request, write=True)
    count = await memory_store.delete_all(name)
    return {"success": True, "data": {"deleted": count}}
