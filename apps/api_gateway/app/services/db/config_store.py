from __future__ import annotations

import logging
import os
import threading
from typing import Callable, Generic, TypeVar

from pydantic import BaseModel
from sqlalchemy import delete as sa_delete, select

from app.core.settings import settings
from app.services.db.sync_engine import init_config_tables, session_scope

logger = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)


class SqliteBackedStore(Generic[M]):
    """Keyed config store: in-memory cache + write-through to a (name, data) table.

    Subclass/parameterize with the SQLAlchemy row class, the Pydantic model, the
    model's key attribute, and a callable that parses a legacy-JSON dict of
    {name: raw_dict} for the one-time import (raw, not-yet-validated, so each
    record can be validated -- and skipped on failure -- individually).

    Path resolution: `path`, if given, is used verbatim (this is what direct,
    explicit-path construction in tests relies on). If omitted, `settings_attr`
    names an attribute on `app.core.settings.settings` that is re-read *at
    load time* (inside `_ensure`), never captured once at construction time.
    This matters for the module-level singletons (`profile_store` etc.): they
    are built once, at import time -- typically during test collection,
    before any fixture has a chance to monkeypatch settings. Capturing the
    path eagerly would permanently bind the singleton to whatever path was in
    effect at import (in practice, the real on-disk file), so tests that
    monkeypatch settings later would silently keep hitting the real file.
    """

    def __init__(
        self,
        path: str | None = None,
        *,
        row_cls: type,
        model_cls: type[M],
        key_attr: str,
        legacy_parse: Callable[[str], dict[str, dict]],
        settings_attr: str | None = None,
    ) -> None:
        self._path = path
        self._settings_attr = settings_attr
        self._row = row_cls
        self._model = model_cls
        self._key = key_attr
        self._legacy_parse = legacy_parse
        self._lock = threading.Lock()
        self._cache: dict[str, M] | None = None

    def _resolve_path(self) -> str | None:
        if self._path:
            return self._path
        if self._settings_attr:
            return getattr(settings, self._settings_attr)
        return None

    def _ensure(self) -> None:
        if self._cache is not None:
            return
        init_config_tables()
        with session_scope() as s:
            rows = s.execute(select(self._row)).scalars().all()
            self._cache = {r.name: self._model.model_validate_json(r.data) for r in rows}
        if not self._cache:
            path = self._resolve_path()
            if path and os.path.exists(path):
                self._import_legacy(path)

    def _import_legacy(self, path: str) -> None:
        """One-time, best-effort import of the legacy JSON file into the DB.

        Never destructive: the file is left in place (as a backup) regardless
        of outcome. Records are validated individually -- one malformed entry
        is skipped and logged, it does not abort the import of the rest.
        """
        try:
            raw = self._legacy_parse(path)
        except Exception as exc:
            logger.warning(
                "legacy import: could not read/parse %s (%s); file left untouched, no records imported",
                path, exc,
            )
            return
        imported = 0
        skipped = 0
        for key, value in raw.items():
            try:
                model = self._model.model_validate(value)
            except Exception as exc:
                skipped += 1
                logger.warning("legacy import: skipping malformed record %r from %s: %s", key, path, exc)
                continue
            self._put(model)
            imported += 1
        logger.info(
            "legacy import from %s: %d record(s) imported, %d skipped (file kept as backup)",
            path, imported, skipped,
        )

    def _put(self, model: M) -> None:
        name = getattr(model, self._key)
        with session_scope() as s:
            row = s.get(self._row, name)
            if row is None:
                s.add(self._row(name=name, data=model.model_dump_json()))
            else:
                row.data = model.model_dump_json()
        self._cache[name] = model

    def invalidate(self) -> None:
        """Drop the in-memory cache so the next access re-reads (and re-creates
        the tables of) whatever DB is configured NOW. Same contract as
        ModelRegistryStore.invalidate(): the module-level store singletons
        outlive a DB switch, so tests that repoint the engine at a per-test tmp
        file must invalidate or they keep serving the previous DB's rows."""
        with self._lock:
            self._cache = None

    def list(self) -> dict[str, M]:
        with self._lock:
            self._ensure()
            return dict(self._cache)

    def get(self, name: str) -> M | None:
        with self._lock:
            self._ensure()
            return self._cache.get(name)

    def upsert(self, model: M) -> None:
        with self._lock:
            self._ensure()
            self._put(model)

    def delete(self, name: str) -> None:
        with self._lock:
            self._ensure()
            with session_scope() as s:
                s.execute(sa_delete(self._row).where(self._row.name == name))
            self._cache.pop(name, None)
