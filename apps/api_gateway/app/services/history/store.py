from __future__ import annotations

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select

from app.services.db.engine import db_session
from app.services.db.models import ChatMessage, ChatSession, utcnow


def _session_dict(s: ChatSession) -> dict:
    return {
        "id": s.id,
        "profile_id": s.profile_id,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "ended_at": s.ended_at.isoformat() if s.ended_at else None,
        "meta": s.meta or {},
    }


class SessionStore:
    async def create(self, session_id: str, profile_id: str = "", meta: dict | None = None) -> dict:
        async with db_session() as s:
            row = ChatSession(id=session_id, profile_id=profile_id, meta=meta or {})
            s.add(row)
            await s.commit()
            return _session_dict(row)

    async def get(self, session_id: str) -> dict | None:
        async with db_session() as s:
            row = await s.get(ChatSession, session_id)
            return _session_dict(row) if row else None

    async def exists(self, session_id: str) -> bool:
        return await self.get(session_id) is not None

    async def list(self, profile_id: str | None = None, limit: int = 20, offset: int = 0) -> list[dict]:
        async with db_session() as s:
            q = select(ChatSession).order_by(ChatSession.created_at.desc())
            if profile_id is not None:
                q = q.where(ChatSession.profile_id == profile_id)
            rows = (await s.execute(q.limit(limit).offset(offset))).scalars().all()
            out = []
            for row in rows:
                count = (
                    await s.execute(
                        select(func.count(ChatMessage.id)).where(ChatMessage.session_id == row.id)
                    )
                ).scalar_one()
                first = (
                    await s.execute(
                        select(ChatMessage.content)
                        .where(ChatMessage.session_id == row.id, ChatMessage.role == "user")
                        .order_by(ChatMessage.id)
                        .limit(1)
                    )
                ).scalar_one_or_none()
                d = _session_dict(row)
                d["message_count"] = count
                d["preview"] = (first or "")[:80]
                out.append(d)
            return out

    async def append_message(self, session_id: str, turn: int, role: str, content: str) -> None:
        async with db_session() as s:
            s.add(ChatMessage(session_id=session_id, turn=turn, role=role, content=content))
            await s.commit()

    async def get_messages(self, session_id: str) -> list[dict]:
        async with db_session() as s:
            rows = (
                await s.execute(
                    select(ChatMessage)
                    .where(ChatMessage.session_id == session_id)
                    .order_by(ChatMessage.id)
                )
            ).scalars().all()
            return [{"turn": m.turn, "role": m.role, "content": m.content} for m in rows]

    async def mark_ended(self, session_id: str) -> None:
        async with db_session() as s:
            row = await s.get(ChatSession, session_id)
            if row:
                row.ended_at = utcnow()
                await s.commit()

    async def delete(self, session_id: str) -> bool:
        async with db_session() as s:
            row = await s.get(ChatSession, session_id)
            if not row:
                return False
            await s.execute(sa_delete(ChatMessage).where(ChatMessage.session_id == session_id))
            await s.delete(row)
            await s.commit()
            return True


session_store = SessionStore()
