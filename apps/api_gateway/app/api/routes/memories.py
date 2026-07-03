from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from app.services.memory.store import memory_store

router = APIRouter(prefix="/v1/profiles/{name}/memories", tags=["memories"])


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
async def list_memories(name: str) -> dict:
    return {"success": True, "data": await memory_store.list(name)}


@router.post("")
async def add_memory(name: str, payload: MemoryRequest) -> dict:
    row = await memory_store.add(name, payload.content)
    return {"success": True, "data": row}


@router.put("/{memory_id}")
async def update_memory(name: str, memory_id: str, payload: MemoryRequest) -> dict:
    row = await memory_store.update(memory_id, payload.content, profile_id=name)
    if not row:
        raise HTTPException(status_code=404, detail=f"Memory '{memory_id}' not found")
    return {"success": True, "data": row}


@router.delete("/{memory_id}")
async def delete_memory(name: str, memory_id: str) -> dict:
    if not await memory_store.delete(memory_id, profile_id=name):
        raise HTTPException(status_code=404, detail=f"Memory '{memory_id}' not found")
    return {"success": True, "data": {"id": memory_id, "deleted": True}}


@router.delete("")
async def delete_all_memories(name: str) -> dict:
    count = await memory_store.delete_all(name)
    return {"success": True, "data": {"deleted": count}}
