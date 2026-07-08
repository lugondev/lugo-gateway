from __future__ import annotations

import os
import threading

from pydantic import BaseModel

from app.core.settings import settings
from app.services.db.config_models import SystemRow
from app.services.db.sync_engine import init_config_tables, session_scope

_ROW_ID = 1


class SystemConfig(BaseModel):
    base_context: str = ""


class SystemConfigStore:
    """Singleton config store: one `SystemRow(id=1)` in `config_system`.

    Mirrors the SqliteBackedStore cache + write-through + legacy-import-then-
    delete pattern (see app/services/db/config_store.py), but for a single
    row keyed by id rather than a keyed table.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._cache: SystemConfig | None = None

    def _ensure(self) -> None:
        if self._cache is not None:
            return
        init_config_tables()
        with session_scope() as s:
            row = s.get(SystemRow, _ROW_ID)
            if row is not None:
                self._cache = SystemConfig.model_validate_json(row.data)
        if self._cache is None:
            if self._path and os.path.exists(self._path):
                self._cache = self._import_legacy()
            else:
                self._cache = SystemConfig()

    def _import_legacy(self) -> SystemConfig:
        try:
            config = SystemConfig.model_validate_json(open(self._path).read())
        except Exception:
            config = SystemConfig()
        self._put(config)
        try:
            os.remove(self._path)
        except OSError:
            pass
        return config

    def _put(self, config: SystemConfig) -> None:
        with session_scope() as s:
            row = s.get(SystemRow, _ROW_ID)
            if row is None:
                s.add(SystemRow(id=_ROW_ID, data=config.model_dump_json()))
            else:
                row.data = config.model_dump_json()

    def get(self) -> SystemConfig:
        with self._lock:
            self._ensure()
            return self._cache

    def set_base_context(self, value: str) -> SystemConfig:
        with self._lock:
            self._ensure()
            config = SystemConfig(base_context=value)
            self._put(config)
            self._cache = config
            return config


system_config_store = SystemConfigStore(settings.system_config_path)
