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
        "source": s.source or "",
        "client_id": s.client_id or "",
    }


class SessionStore:
    async def create(
        self, session_id: str, profile_id: str = "", meta: dict | None = None,
        user_id: str | None = None, source: str = "", client_id: str = "",
    ) -> dict:
        async with db_session() as s:
            row = ChatSession(
                id=session_id, profile_id=profile_id, meta=meta or {}, user_id=user_id,
                source=source, client_id=client_id,
            )
            s.add(row)
            await s.commit()
            return _session_dict(row)

    async def latest_for_client(self, source: str, client_id: str) -> dict | None:
        """The newest conversation belonging to one client, or None.

        This is what a reconnecting device continues. Scoped to the client rather
        than the user on purpose: "the user's latest" handed the browser whatever
        the speaker had just been saying, and handed the speaker nothing at all
        (it remembers no id). Blank provenance is never matched -- rows written
        before these columns existed are not guessed into anyone's thread.
        """
        if not source or not client_id:
            return None
        async with db_session() as s:
            row = (
                await s.execute(
                    select(ChatSession)
                    .where(ChatSession.source == source, ChatSession.client_id == client_id)
                    .order_by(ChatSession.created_at.desc())
                    .limit(1)
                )
            ).scalars().first()
            return _session_dict(row) if row else None

    async def reopen(self, session_id: str) -> None:
        """Clear ended_at: this conversation is live again.

        Without it a resumed session reads as ended in History while it is being
        added to, and close() would just set the same field again at the end."""
        async with db_session() as s:
            row = await s.get(ChatSession, session_id)
            if row is not None and row.ended_at is not None:
                row.ended_at = None
                await s.commit()

    async def get(self, session_id: str) -> dict | None:
        async with db_session() as s:
            row = await s.get(ChatSession, session_id)
            return _session_dict(row) if row else None

    async def exists(self, session_id: str) -> bool:
        return await self.get(session_id) is not None

    async def list(
        self, profile_id: str | None = None, user_id: str | None = None,
        limit: int = 20, offset: int = 0, source: str | None = None,
        client_id: str | None = None,
    ) -> list[dict]:
        async with db_session() as s:
            q = select(ChatSession).order_by(ChatSession.created_at.desc())
            if profile_id is not None:
                q = q.where(ChatSession.profile_id == profile_id)
            if user_id is not None:
                q = q.where(ChatSession.user_id == user_id)
            if source is not None:
                q = q.where(ChatSession.source == source)
            if client_id is not None:
                q = q.where(ChatSession.client_id == client_id)
            rows = (await s.execute(q.limit(limit).offset(offset))).scalars().all()
            if not rows:
                return []
            # Three queries, not 1 + 2N. This used to run a COUNT and a preview
            # SELECT per session inside the loop -- 41 round trips for the
            # default page of 20, each opening its own SQLite connection (the
            # engine runs NullPool on purpose; see db/engine.py).
            ids = [row.id for row in rows]
            counts = dict(
                (
                    await s.execute(
                        select(ChatMessage.session_id, func.count(ChatMessage.id))
                        .where(ChatMessage.session_id.in_(ids))
                        .group_by(ChatMessage.session_id)
                    )
                ).all()
            )
            # The first user message of each session, in two steps: its id per
            # session, then those rows' content. min(id) is the same ordering
            # the per-row `order_by(id).limit(1)` used.
            first_ids = [
                mid
                for _sid, mid in (
                    await s.execute(
                        select(ChatMessage.session_id, func.min(ChatMessage.id))
                        .where(ChatMessage.session_id.in_(ids), ChatMessage.role == "user")
                        .group_by(ChatMessage.session_id)
                    )
                ).all()
            ]
            previews: dict[str, str] = {}
            if first_ids:
                previews = {
                    sid: content
                    for sid, content in (
                        await s.execute(
                            select(ChatMessage.session_id, ChatMessage.content).where(
                                ChatMessage.id.in_(first_ids)
                            )
                        )
                    ).all()
                }
            out = []
            for row in rows:
                d = _session_dict(row)
                d["message_count"] = counts.get(row.id, 0)
                d["preview"] = (previews.get(row.id) or "")[:80]
                out.append(d)
            return out

    async def count(
        self, profile_id: str | None = None, user_id: str | None = None,
        source: str | None = None, client_id: str | None = None,
    ) -> int:
        async with db_session() as s:
            q = select(func.count()).select_from(ChatSession)
            if profile_id is not None:
                q = q.where(ChatSession.profile_id == profile_id)
            if user_id is not None:
                q = q.where(ChatSession.user_id == user_id)
            if source is not None:
                q = q.where(ChatSession.source == source)
            if client_id is not None:
                q = q.where(ChatSession.client_id == client_id)
            return (await s.execute(q)).scalar_one()

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
