from __future__ import annotations

import hashlib
import secrets
import uuid

from sqlalchemy import select

from app.services.db.engine import db_session
from app.services.db.models import Device, utcnow


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _device_dict(d: Device) -> dict:
    return {
        "id": d.id,
        "user_id": d.user_id,
        "name": d.name,
        "serial": d.serial,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None,
        "revoked": d.revoked,
    }


class DeviceStore:
    async def find_active_by_serial(self, serial: str) -> Device | None:
        async with db_session() as s:
            return (
                await s.execute(
                    select(Device).where(Device.serial == serial, Device.revoked.is_(False))
                )
            ).scalar_one_or_none()

    async def create(self, user_id: str, name: str, serial: str) -> tuple[dict, str]:
        """Returns (device_dict, raw_token). The raw token exists only here and in
        the caller's response -- only its hash is ever persisted."""
        raw_token = secrets.token_urlsafe(32)
        async with db_session() as s:
            row = Device(
                id=str(uuid.uuid4()), user_id=user_id, name=name, serial=serial,
                token_hash=_hash_token(raw_token),
            )
            s.add(row)
            await s.commit()
            return _device_dict(row), raw_token

    async def get_by_token(self, raw_token: str) -> Device | None:
        async with db_session() as s:
            return (
                await s.execute(select(Device).where(Device.token_hash == _hash_token(raw_token)))
            ).scalar_one_or_none()

    async def get_by_id(self, device_id: str) -> Device | None:
        async with db_session() as s:
            return await s.get(Device, device_id)

    async def list_for_user(self, user_id: str) -> list[dict]:
        async with db_session() as s:
            rows = (
                await s.execute(
                    select(Device).where(Device.user_id == user_id).order_by(Device.created_at)
                )
            ).scalars().all()
            return [_device_dict(d) for d in rows]

    async def list_all(self) -> list[dict]:
        async with db_session() as s:
            rows = (await s.execute(select(Device).order_by(Device.created_at))).scalars().all()
            return [_device_dict(d) for d in rows]

    async def revoke(self, device_id: str, owner_user_id: str | None = None) -> bool:
        """owner_user_id restricts revoke to devices owned by that user (the
        'mine' route); None means any device (the admin route)."""
        async with db_session() as s:
            row = await s.get(Device, device_id)
            if row is None:
                return False
            if owner_user_id is not None and row.user_id != owner_user_id:
                return False
            row.revoked = True
            await s.commit()
            return True

    async def touch_last_seen(self, device_id: str) -> None:
        async with db_session() as s:
            row = await s.get(Device, device_id)
            if row is not None:
                row.last_seen_at = utcnow()
                await s.commit()


device_store = DeviceStore()
