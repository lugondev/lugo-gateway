from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select

from app.services.db.engine import db_session
from app.services.db.models import Provider


def _entry_dict(p: Provider) -> dict:
    return {
        "id": p.id, "name": p.name, "label": p.label, "base_url": p.base_url,
        "api_key": p.api_key, "enabled": p.enabled, "config": p.config or {},
    }


def _copy(entry: dict) -> dict:
    """Detached copy: routes mask api_key on what they get back, and mutating
    the cached object would corrupt the real key for later get_sync() calls."""
    out = dict(entry)
    out["config"] = dict(entry["config"])
    return out


class ProviderStore:
    """In-memory cache (keyed by id) + write-through to DB. Same pattern as
    ModelRegistryStore: `get_sync` serves off-event-loop callers (provider
    build via asyncio.to_thread) and returns None when the cache is cold."""

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
                rows = (await s.execute(select(Provider))).scalars().all()
            self._by_id = {p.id: _entry_dict(p) for p in rows}

    def invalidate(self) -> None:
        self._by_id = None
        self._lock = asyncio.Lock()

    async def list_all(self) -> list[dict]:
        await self._ensure_loaded()
        entries = sorted(self._by_id.values(), key=lambda e: (e["name"], e["id"]))
        return [_copy(e) for e in entries]

    async def get(self, provider_id: str) -> dict | None:
        await self._ensure_loaded()
        entry = self._by_id.get(provider_id)
        return None if entry is None else _copy(entry)

    def get_sync(self, provider_id: str) -> dict | None:
        by_id = self._by_id
        if by_id is None:
            return None
        entry = by_id.get(provider_id)
        return None if entry is None else _copy(entry)

    async def create(self, name: str, label: str = "", base_url: str = "",
                     api_key: str = "", enabled: bool = True,
                     config: dict | None = None) -> dict:
        await self._ensure_loaded()
        async with db_session() as s:
            row = Provider(id=str(uuid.uuid4()), name=name, label=label, base_url=base_url,
                           api_key=api_key, enabled=enabled, config=config or {})
            s.add(row)
            await s.commit()
            entry = _entry_dict(row)
        self._by_id[entry["id"]] = entry
        return _copy(entry)

    async def set_fields(self, provider_id: str, **fields) -> dict | None:
        await self._ensure_loaded()
        async with db_session() as s:
            row = await s.get(Provider, provider_id)
            if row is None:
                return None
            for key, value in fields.items():
                setattr(row, key, value)
            await s.commit()
            entry = _entry_dict(row)
        self._by_id[provider_id] = entry
        return _copy(entry)

    async def delete(self, provider_id: str) -> bool:
        await self._ensure_loaded()
        async with db_session() as s:
            row = await s.get(Provider, provider_id)
            if row is None:
                return False
            await s.delete(row)
            await s.commit()
        self._by_id.pop(provider_id, None)
        return True


provider_store = ProviderStore()
