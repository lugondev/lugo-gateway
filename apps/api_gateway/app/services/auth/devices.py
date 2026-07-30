from __future__ import annotations

import hashlib
import secrets
import uuid

from sqlalchemy import select

from app.core.timefmt import iso_utc
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
        # `or ""` because the column was added to an existing table by
        # _ensure_column (db/engine.py): rows written before that ALTER read
        # back as NULL on some drivers, and every consumer treats "" as
        # "unassigned". Never let None reach the API.
        "profile_id": d.profile_id or "",
        "created_at": iso_utc(d.created_at),
        "last_seen_at": iso_utc(d.last_seen_at),
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

    async def create(
        self, user_id: str, name: str, serial: str, profile_id: str = ""
    ) -> tuple[dict, str]:
        """Returns (device_dict, raw_token). The raw token exists only here and in
        the caller's response -- only its hash is ever persisted.

        `profile_id` is set at creation rather than by a follow-up assign call so
        that pairing from inside a profile is atomic: there is no window in which
        the device exists but answers to no assistant. The CALLER must have
        already checked the profile is visible to `user_id` (see
        services/profile_visibility.py) -- this store does no visibility work."""
        raw_token = secrets.token_urlsafe(32)
        async with db_session() as s:
            row = Device(
                id=str(uuid.uuid4()), user_id=user_id, name=name, serial=serial,
                profile_id=profile_id, token_hash=_hash_token(raw_token),
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

    async def set_profile(self, device_id: str, profile_id: str, owner_user_id: str) -> bool:
        """Bind (or, with profile_id="", unbind) a device the caller owns.

        Same ownership shape as revoke(): a device_id belonging to someone else
        returns False and is reported by the route as 404, so this cannot be used
        to probe which device ids exist. `owner_user_id` is required here -- unlike
        revoke there is no admin caller for this, and a None default would make an
        unscoped rebind one forgotten argument away.

        Visibility of `profile_id` itself is the CALLER's job (routes/devices.py):
        binding to a profile the owner cannot see would hand them that profile's
        llm.api_key and system_prompt at connect time."""
        async with db_session() as s:
            row = await s.get(Device, device_id)
            if row is None or row.user_id != owner_user_id or row.revoked:
                return False
            row.profile_id = profile_id
            await s.commit()
            return True

    async def clear_profile(self, profile_id: str) -> int:
        """Unbind every device pointing at `profile_id`; returns how many.

        Called when a profile is deleted. Devices are NOT revoked -- the pairing
        token is hardware identity, the profile is a soft setting, so losing the
        assistant must never cost the user a physical re-pairing trip. They land
        in the "Unassigned" group instead."""
        if not profile_id:
            return 0
        async with db_session() as s:
            rows = (
                await s.execute(select(Device).where(Device.profile_id == profile_id))
            ).scalars().all()
            for row in rows:
                row.profile_id = ""
            await s.commit()
            return len(rows)

    async def touch_last_seen(self, device_id: str) -> None:
        async with db_session() as s:
            row = await s.get(Device, device_id)
            if row is not None:
                row.last_seen_at = utcnow()
                await s.commit()


device_store = DeviceStore()
