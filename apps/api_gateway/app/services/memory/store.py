from __future__ import annotations

import uuid

from sqlalchemy import delete as sa_delete
from sqlalchemy import select

from app.services.db.engine import db_session
from app.services.db.models import MemoryItem, utcnow


def _mem_dict(m: MemoryItem) -> dict:
    return {
        "id": m.id,
        "profile_id": m.profile_id,
        "content": m.content,
        "source_session_id": m.source_session_id,
        "embedding": m.embedding,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }


class MemoryStore:
    async def list(self, profile_id: str) -> list[dict]:
        async with db_session() as s:
            rows = (
                await s.execute(
                    select(MemoryItem)
                    .where(MemoryItem.profile_id == profile_id)
                    .order_by(MemoryItem.created_at.desc(), MemoryItem.id)
                )
            ).scalars().all()
            return [_mem_dict(m) for m in rows]

    async def add(
        self,
        profile_id: str,
        content: str,
        source_session_id: str | None = None,
        embedding: list[float] | None = None,
    ) -> dict:
        async with db_session() as s:
            row = MemoryItem(
                id=str(uuid.uuid4()),
                profile_id=profile_id,
                content=content,
                source_session_id=source_session_id,
                embedding=embedding,
            )
            s.add(row)
            await s.commit()
            return _mem_dict(row)

    async def update(self, memory_id: str, content: str, profile_id: str | None = None) -> dict | None:
        async with db_session() as s:
            row = await s.get(MemoryItem, memory_id)
            if not row or (profile_id is not None and row.profile_id != profile_id):
                return None
            row.content = content
            row.updated_at = utcnow()
            await s.commit()
            return _mem_dict(row)

    async def delete(self, memory_id: str, profile_id: str | None = None) -> bool:
        async with db_session() as s:
            row = await s.get(MemoryItem, memory_id)
            if not row or (profile_id is not None and row.profile_id != profile_id):
                return False
            await s.delete(row)
            await s.commit()
            return True

    async def delete_all(self, profile_id: str) -> int:
        async with db_session() as s:
            result = await s.execute(
                sa_delete(MemoryItem).where(MemoryItem.profile_id == profile_id)
            )
            await s.commit()
            return result.rowcount or 0


memory_store = MemoryStore()
