from __future__ import annotations

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select

from app.core.timefmt import iso_utc
from app.services.db.engine import db_session
from app.services.db.models import ChatMessage, ChatSession, utcnow


def _session_dict(s: ChatSession) -> dict:
    return {
        "id": s.id,
        "profile_id": s.profile_id,
        "user_id": s.user_id,
        "created_at": iso_utc(s.created_at),
        "ended_at": iso_utc(s.ended_at),
        "meta": s.meta or {},
    }


class SessionStore:
    async def create(
        self, session_id: str, profile_id: str = "", meta: dict | None = None,
        user_id: str | None = None,
    ) -> dict:
        async with db_session() as s:
            row = ChatSession(id=session_id, profile_id=profile_id, meta=meta or {}, user_id=user_id)
            s.add(row)
            await s.commit()
            return _session_dict(row)

    async def get(self, session_id: str) -> dict | None:
        async with db_session() as s:
            row = await s.get(ChatSession, session_id)
            return _session_dict(row) if row else None

    async def exists(self, session_id: str) -> bool:
        return await self.get(session_id) is not None

    async def list(
        self, profile_id: str | None = None, user_id: str | None = None,
        limit: int = 20, offset: int = 0,
    ) -> list[dict]:
        async with db_session() as s:
            q = select(ChatSession).order_by(ChatSession.created_at.desc())
            if profile_id is not None:
                q = q.where(ChatSession.profile_id == profile_id)
            if user_id is not None:
                q = q.where(ChatSession.user_id == user_id)
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
            # created_at goes out too: the web client shows when each turn was
            # said. iso_utc, not the raw column -- a naive SQLite datetime is
            # read as LOCAL time by JS and lands hours off (see timefmt).
            return [
                {
                    "turn": m.turn,
                    "role": m.role,
                    "content": m.content,
                    "created_at": iso_utc(m.created_at),
                }
                for m in rows
            ]

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

    async def delete_many(self, ids: list[str]) -> int:
        """Delete the given sessions (and their messages). Missing IDs are skipped.
        Returns the number of sessions actually deleted."""
        if not ids:
            return 0
        async with db_session() as s:
            existing = (
                await s.execute(select(ChatSession.id).where(ChatSession.id.in_(ids)))
            ).scalars().all()
            if not existing:
                return 0
            await s.execute(sa_delete(ChatMessage).where(ChatMessage.session_id.in_(existing)))
            await s.execute(sa_delete(ChatSession).where(ChatSession.id.in_(existing)))
            await s.commit()
            return len(existing)

    async def clear(self, profile_id: str | None = None, only_empty: bool = False) -> int:
        """Delete sessions in scope (and their messages). Returns the count deleted.

        profile_id None => all profiles; otherwise only that profile. only_empty
        restricts to sessions that have zero messages."""
        async with db_session() as s:
            q = select(ChatSession.id)
            if profile_id is not None:
                q = q.where(ChatSession.profile_id == profile_id)
            if only_empty:
                q = q.where(
                    ~select(ChatMessage.id)
                    .where(ChatMessage.session_id == ChatSession.id)
                    .exists()
                )
            ids = (await s.execute(q)).scalars().all()
            if not ids:
                return 0
            await s.execute(sa_delete(ChatMessage).where(ChatMessage.session_id.in_(ids)))
            await s.execute(sa_delete(ChatSession).where(ChatSession.id.in_(ids)))
            await s.commit()
            return len(ids)


session_store = SessionStore()
