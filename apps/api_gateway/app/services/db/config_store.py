from __future__ import annotations

import os
import threading
from typing import Callable, Generic, TypeVar

from pydantic import BaseModel
from sqlalchemy import delete as sa_delete, select

from app.services.db.sync_engine import init_config_tables, session_scope

M = TypeVar("M", bound=BaseModel)


class SqliteBackedStore(Generic[M]):
    """Keyed config store: in-memory cache + write-through to a (name, data) table.

    Subclass/parameterize with the SQLAlchemy row class, the Pydantic model, the
    model's key attribute, and a callable that parses a legacy-JSON dict of
    {name: model_dict} for the one-time import.
    """

    def __init__(
        self,
        path: str,
        *,
        row_cls: type,
        model_cls: type[M],
        key_attr: str,
        legacy_parse: Callable[[str], dict[str, M]],
    ) -> None:
        self._path = path
        self._row = row_cls
        self._model = model_cls
        self._key = key_attr
        self._legacy_parse = legacy_parse
        self._lock = threading.Lock()
        self._cache: dict[str, M] | None = None

    def _ensure(self) -> None:
        if self._cache is not None:
            return
        init_config_tables()
        with session_scope() as s:
            rows = s.execute(select(self._row)).scalars().all()
            self._cache = {r.name: self._model.model_validate_json(r.data) for r in rows}
        if not self._cache and self._path and os.path.exists(self._path):
            self._import_legacy()

    def _import_legacy(self) -> None:
        try:
            seed = self._legacy_parse(self._path)
        except Exception:
            seed = {}
        for model in seed.values():
            self._put(model)
        try:
            os.remove(self._path)
        except OSError:
            pass

    def _put(self, model: M) -> None:
        name = getattr(model, self._key)
        with session_scope() as s:
            row = s.get(self._row, name)
            if row is None:
                s.add(self._row(name=name, data=model.model_dump_json()))
            else:
                row.data = model.model_dump_json()
        self._cache[name] = model

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
