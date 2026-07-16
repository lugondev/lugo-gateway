from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.errors import UsernameTakenError
from app.services.auth.password import hash_password, verify_password
from app.services.db.engine import db_session
from app.services.db.models import User


def _user_dict(u: User) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "role": u.role,
        "can_use_testing": u.can_use_testing,
        "disabled": u.disabled,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


class UserStore:
    async def count(self) -> int:
        async with db_session() as s:
            rows = (await s.execute(select(User.id))).scalars().all()
            return len(rows)

    async def count_active_admins(self) -> int:
        async with db_session() as s:
            rows = (
                await s.execute(
                    select(User.id).where(User.role == "admin", User.disabled.is_(False))
                )
            ).scalars().all()
            return len(rows)

    async def create(self, username: str, password: str, role: str = "user") -> dict:
        # Cheap fast-fail for the common duplicate case, in its own session.
        if await self.get_by_username(username) is not None:
            raise UsernameTakenError(f"username '{username}' is already taken")
        # PBKDF2 at 600k iterations is ~100-300ms of pure CPU -- off the loop
        # (or every hash stalls all live voice sessions) and OUTSIDE the db
        # session (or a signup burst pins connections/SQLite write slots for
        # the whole hash).
        password_hash = await asyncio.to_thread(hash_password, password)
        async with db_session() as s:
            row = User(
                id=str(uuid.uuid4()), username=username,
                password_hash=password_hash, role=role,
            )
            s.add(row)
            try:
                await s.commit()
            except IntegrityError as exc:
                # A concurrent signup won the race during the hash window; the
                # unique constraint on username is the authoritative check.
                raise UsernameTakenError(f"username '{username}' is already taken") from exc
            return _user_dict(row)

    async def get_by_username(self, username: str) -> User | None:
        async with db_session() as s:
            return (
                await s.execute(select(User).where(User.username == username))
            ).scalar_one_or_none()

    async def get_by_id(self, user_id: str) -> User | None:
        async with db_session() as s:
            return await s.get(User, user_id)

    async def verify_login(self, username: str, password: str) -> User | None:
        user = await self.get_by_username(username)
        if user is None:
            return None
        if not await asyncio.to_thread(verify_password, password, user.password_hash):
            return None
        return user

    async def list(self) -> list[dict]:
        async with db_session() as s:
            rows = (await s.execute(select(User).order_by(User.username))).scalars().all()
            return [_user_dict(u) for u in rows]

    async def set_fields(self, user_id: str, **fields) -> dict | None:
        async with db_session() as s:
            row = await s.get(User, user_id)
            if row is None:
                return None
            for key, value in fields.items():
                setattr(row, key, value)
            await s.commit()
            return _user_dict(row)

    async def reset_password(self, user_id: str, new_password: str) -> bool:
        # Hash before opening the session so the connection isn't pinned for
        # the 100-300ms PBKDF2 run.
        password_hash = await asyncio.to_thread(hash_password, new_password)
        async with db_session() as s:
            row = await s.get(User, user_id)
            if row is None:
                return False
            row.password_hash = password_hash
            await s.commit()
            return True


user_store = UserStore()
