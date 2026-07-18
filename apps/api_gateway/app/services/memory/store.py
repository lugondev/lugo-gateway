from __future__ import annotations

import uuid

from sqlalchemy import delete as sa_delete
from sqlalchemy import select

from app.core.timefmt import iso_utc
from app.services.db.engine import db_session
from app.services.db.models import MemoryItem, MemoryProfileDoc, utcnow


def _uid(user_id: str | None) -> str:
    return user_id or ""


def _mem_dict(m: MemoryItem) -> dict:
    return {
        "id": m.id,
        "profile_id": m.profile_id,
        "user_id": m.user_id,
        "content": m.content,
        "source_session_id": m.source_session_id,
        "embedding": m.embedding,
        "created_at": iso_utc(m.created_at),
        "updated_at": iso_utc(m.updated_at),
    }


class MemoryStore:
    async def list(self, profile_id: str, user_id: str | None = None) -> list[dict]:
        async with db_session() as s:
            rows = (
                await s.execute(
                    select(MemoryItem)
                    .where(
                        MemoryItem.profile_id == profile_id,
                        MemoryItem.user_id == _uid(user_id),
                    )
                    .order_by(MemoryItem.created_at.desc(), MemoryItem.id)
                )
            ).scalars().all()
            return [_mem_dict(m) for m in rows]

    async def add(
        self,
        profile_id: str,
        content: str,
        *,
        source_session_id: str | None = None,
        embedding: list[float] | None = None,
        user_id: str | None = None,
    ) -> dict:
        async with db_session() as s:
            row = MemoryItem(
                id=str(uuid.uuid4()),
                profile_id=profile_id,
                content=content,
                source_session_id=source_session_id,
                embedding=embedding,
                user_id=_uid(user_id),
            )
            s.add(row)
            await s.commit()
            return _mem_dict(row)

    async def update(
        self, memory_id: str, content: str, *,
        profile_id: str | None = None, user_id: str | None = None,
    ) -> dict | None:
        async with db_session() as s:
            row = await s.get(MemoryItem, memory_id)
            if not row:
                return None
            if profile_id is not None and row.profile_id != profile_id:
                return None
            if user_id is not None and row.user_id != _uid(user_id):
                return None
            row.content = content
            row.updated_at = utcnow()
            await s.commit()
            return _mem_dict(row)

    async def delete(
        self, memory_id: str, *,
        profile_id: str | None = None, user_id: str | None = None,
    ) -> bool:
        async with db_session() as s:
            row = await s.get(MemoryItem, memory_id)
            if not row:
                return False
            if profile_id is not None and row.profile_id != profile_id:
                return False
            if user_id is not None and row.user_id != _uid(user_id):
                return False
            await s.delete(row)
            await s.commit()
            return True

    async def delete_all(self, profile_id: str, user_id: str | None = None) -> int:
        async with db_session() as s:
            result = await s.execute(
                sa_delete(MemoryItem).where(
                    MemoryItem.profile_id == profile_id,
                    MemoryItem.user_id == _uid(user_id),
                )
            )
            await s.commit()
            return result.rowcount or 0

    async def delete_many(self, ids: list[str]) -> int:
        if not ids:
            return 0
        async with db_session() as s:
            result = await s.execute(sa_delete(MemoryItem).where(MemoryItem.id.in_(ids)))
            await s.commit()
            return result.rowcount or 0


memory_store = MemoryStore()


def _doc_dict(d: MemoryProfileDoc) -> dict:
    return {
        "profile_id": d.profile_id,
        "user_id": d.user_id,
        "content": d.content,
        "updated_at": iso_utc(d.updated_at),
    }


class ProfileDocStore:
    async def get(self, profile_id: str, user_id: str | None = None) -> dict | None:
        async with db_session() as s:
            row = await s.get(MemoryProfileDoc, (_uid(user_id), profile_id))
            return _doc_dict(row) if row else None

    async def upsert(self, profile_id: str, content: str, user_id: str | None = None) -> dict:
        async with db_session() as s:
            row = await s.get(MemoryProfileDoc, (_uid(user_id), profile_id))
            if row is None:
                row = MemoryProfileDoc(profile_id=profile_id, content=content, user_id=_uid(user_id))
                s.add(row)
            else:
                row.content = content
                row.updated_at = utcnow()
            await s.commit()
            return _doc_dict(row)

    async def delete(self, profile_id: str, user_id: str | None = None) -> bool:
        async with db_session() as s:
            row = await s.get(MemoryProfileDoc, (_uid(user_id), profile_id))
            if row is None:
                return False
            await s.delete(row)
            await s.commit()
            return True


profile_doc_store = ProfileDocStore()
