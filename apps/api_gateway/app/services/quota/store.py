from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select

from app.services.db.engine import db_session
from app.services.db.models import Quota


def _entry_dict(q: Quota) -> dict:
    return {
        "id": q.id, "scope": q.scope, "scope_id": q.scope_id,
        "limit_usd": q.limit_usd, "period": q.period, "enabled": q.enabled,
    }


def _copy(entry: dict) -> dict:
    """Detached copy so mutating what callers get back never corrupts the
    cached object (same rationale as ProviderStore._copy)."""
    return dict(entry)


class QuotaStore:
    """In-memory cache (keyed by id) + write-through to DB. Mirrors
    ProviderStore's pattern."""

    def __init__(self) -> None:
        self._by_id: dict[str, dict] | None = None
        self._lock = asyncio.Lock()

    async def _ensure_loaded(self) -> None:
        if self._by_id is not None:
            return
        async with self._lock:
            if self._by_id is not None:
                return
            async with db_session() as s:
                rows = (await s.execute(select(Quota))).scalars().all()
            self._by_id = {q.id: _entry_dict(q) for q in rows}

    def invalidate(self) -> None:
        self._by_id = None
        self._lock = asyncio.Lock()

    async def list_all(self) -> list[dict]:
        await self._ensure_loaded()
        entries = sorted(self._by_id.values(), key=lambda e: (e["scope"], e["scope_id"], e["id"]))
        return [_copy(e) for e in entries]

    async def list_enabled(self) -> list[dict]:
        entries = await self.list_all()
        return [e for e in entries if e["enabled"]]

    async def get(self, quota_id: str) -> dict | None:
        await self._ensure_loaded()
        entry = self._by_id.get(quota_id)
        return None if entry is None else _copy(entry)

    async def create(self, scope: str, scope_id: str = "", limit_usd: float = 0.0,
                     period: str = "monthly", enabled: bool = True) -> dict:
        await self._ensure_loaded()
        async with db_session() as s:
            row = Quota(id=str(uuid.uuid4()), scope=scope, scope_id=scope_id,
                       limit_usd=limit_usd, period=period, enabled=enabled)
            s.add(row)
            await s.commit()
            entry = _entry_dict(row)
        self._by_id[entry["id"]] = entry
        return _copy(entry)

    async def set_fields(self, quota_id: str, **fields) -> dict | None:
        await self._ensure_loaded()
        async with db_session() as s:
            row = await s.get(Quota, quota_id)
            if row is None:
                return None
            for key, value in fields.items():
                setattr(row, key, value)
            await s.commit()
            entry = _entry_dict(row)
        self._by_id[quota_id] = entry
        return _copy(entry)

    async def delete(self, quota_id: str) -> bool:
        await self._ensure_loaded()
        async with db_session() as s:
            row = await s.get(Quota, quota_id)
            if row is None:
                return False
            await s.delete(row)
            await s.commit()
        self._by_id.pop(quota_id, None)
        return True


quota_store = QuotaStore()
