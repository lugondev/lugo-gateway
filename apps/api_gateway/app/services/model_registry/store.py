from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select

from app.services.db.engine import db_session
from app.services.db.models import ModelRegistryEntry


def _entry_dict(e: ModelRegistryEntry) -> dict:
    return {
        "id": e.id, "kind": e.kind, "engine": e.engine, "model_id": e.model_id,
        "label": e.label, "enabled": e.enabled, "stage": e.stage,
        "api_key": e.api_key, "base_url": e.base_url, "config": e.config or {},
    }


class ModelRegistryStore:
    """In-memory cache (keyed by row id) + write-through to the DB, so a hot
    path like `find()` (called once per STT/LLM request to resolve a model's
    api_key -- see openrouter_provider.py/responder.py) doesn't hit the DB on
    every single call. Loaded lazily on first access; `create`/`set_fields`
    update the cache in the same call that writes through, so readers never
    see stale data from their own writes. `find`/`get`/`has_key_for_engine`
    now return plain dicts (not the SQLAlchemy row), matching what's cached.

    Registry size is small (tens of models, not thousands), so a full-scan
    `find()`/`has_key_for_engine()` over the cached dict is fine -- no need
    for a second index keyed by (kind, engine, model_id).
    """

    def __init__(self) -> None:
        self._by_id: dict[str, dict] | None = None
        self._lock = asyncio.Lock()

    async def _ensure_loaded(self) -> None:
        if self._by_id is not None:
            return
        async with self._lock:
            if self._by_id is not None:  # lost the race to another awaiter
                return
            async with db_session() as s:
                rows = (await s.execute(select(ModelRegistryEntry))).scalars().all()
            self._by_id = {e.id: _entry_dict(e) for e in rows}

    def invalidate(self) -> None:
        """Force the next access to reload from the DB. Tests call this when
        they repoint the DB at a fresh file (see tests/conftest.py::_tmp_db) --
        without it, a lazily-loaded cache from a previous test's DB would
        silently leak into the next one.

        Also replaces `_lock`: each pytest-asyncio test runs its own event
        loop, and an `asyncio.Lock` first acquired under a now-closed loop
        raises "bound to a different event loop" (or, depending on timing,
        hangs) if a later test's loop tries to acquire it -- see
        asyncio.mixins._LoopBoundMixin. A fresh Lock binds to whichever loop
        acquires it next."""
        self._by_id = None
        self._lock = asyncio.Lock()

    async def list_all(self) -> list[dict]:
        await self._ensure_loaded()
        return sorted(self._by_id.values(), key=lambda e: (e["kind"], e["engine"], e["model_id"]))

    async def find(self, kind: str, engine: str, model_id: str) -> dict | None:
        await self._ensure_loaded()
        for entry in self._by_id.values():
            if entry["kind"] == kind and entry["engine"] == engine and entry["model_id"] == model_id:
                return entry
        return None

    async def get(self, entry_id: str) -> dict | None:
        await self._ensure_loaded()
        return self._by_id.get(entry_id)

    async def find_enabled(self, kind: str, engine: str | None = None) -> dict | None:
        """The single enabled entry for this kind (optionally scoped to one
        engine) -- the "active" config source convention used by kinds with
        no per-profile selection (e.g. the default conversation LLM). If more
        than one entry is enabled, the first found wins; callers should treat
        multiple-enabled as a UI misconfiguration to fix, not rely on the
        tie-break order."""
        await self._ensure_loaded()
        for entry in self._by_id.values():
            if entry["kind"] == kind and entry["enabled"] and (engine is None or entry["engine"] == engine):
                return entry
        return None

    def find_sync(self, kind: str, engine: str, model_id: str) -> dict | None:
        """Synchronous, cache-only equivalent of `find()` for call sites that
        can't `await` -- provider code that builds a model off the event loop
        via `asyncio.to_thread`, or module-level singletons constructed at
        import time before anything has awaited this store. Returns None if
        the cache hasn't been warmed yet (nothing has awaited any store
        method since process start / last `invalidate()`) -- callers must
        treat that exactly like "no matching entry", the same fallback they
        already need for a genuinely-missing entry."""
        if self._by_id is None:
            return None
        for entry in self._by_id.values():
            if entry["kind"] == kind and entry["engine"] == engine and entry["model_id"] == model_id:
                return entry
        return None

    def find_enabled_sync(self, kind: str, engine: str | None = None) -> dict | None:
        """Synchronous, cache-only equivalent of `find_enabled()` -- see
        `find_sync` for why a sync path is needed."""
        if self._by_id is None:
            return None
        for entry in self._by_id.values():
            if entry["kind"] == kind and entry["enabled"] and (engine is None or entry["engine"] == engine):
                return entry
        return None

    async def has_key_for_engine(self, kind: str, engine: str) -> bool:
        """True if any enabled entry for this (kind, engine) has a non-empty api_key."""
        await self._ensure_loaded()
        return any(
            e["api_key"]
            for e in self._by_id.values()
            if e["kind"] == kind and e["engine"] == engine and e["enabled"]
        )

    async def create(
        self,
        kind: str,
        engine: str,
        model_id: str,
        label: str,
        stage: str = "stable",
        api_key: str = "",
        base_url: str = "",
        config: dict | None = None,
        enabled: bool = True,
    ) -> dict:
        await self._ensure_loaded()
        async with db_session() as s:
            row = ModelRegistryEntry(
                id=str(uuid.uuid4()), kind=kind, engine=engine, model_id=model_id,
                label=label, enabled=enabled, stage=stage, api_key=api_key, base_url=base_url,
                config=config or {},
            )
            s.add(row)
            await s.commit()
            entry = _entry_dict(row)
        self._by_id[entry["id"]] = entry
        return entry

    async def set_fields(self, entry_id: str, **fields) -> dict | None:
        await self._ensure_loaded()
        async with db_session() as s:
            row = await s.get(ModelRegistryEntry, entry_id)
            if row is None:
                return None
            for key, value in fields.items():
                setattr(row, key, value)
            await s.commit()
            entry = _entry_dict(row)
        self._by_id[entry_id] = entry
        return entry


model_registry_store = ModelRegistryStore()
