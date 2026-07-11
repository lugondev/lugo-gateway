from __future__ import annotations

import logging
import os
import threading

from pydantic import BaseModel

from app.core.settings import settings
from app.services.db.config_models import SystemRow
from app.services.db.sync_engine import init_config_tables, session_scope

logger = logging.getLogger(__name__)

_ROW_ID = 1


class SystemConfig(BaseModel):
    base_context: str = ""
    openrouter_api_key: str = ""


class SystemConfigStore:
    """Singleton config store: one `SystemRow(id=1)` in `config_system`.

    Mirrors the SqliteBackedStore cache + write-through + non-destructive
    legacy-import pattern (see app/services/db/config_store.py), but for a
    single row keyed by id rather than a keyed table.

    Path resolution matches SqliteBackedStore: an explicit `path` is used
    verbatim (for tests), otherwise `settings_attr` is re-read from
    `app.core.settings.settings` lazily, at `_ensure()` time -- not captured
    once at construction, since the module-level singleton is built at
    import time, before test fixtures can monkeypatch settings.
    """

    def __init__(self, path: str | None = None, *, settings_attr: str | None = None) -> None:
        self._path = path
        self._settings_attr = settings_attr
        self._lock = threading.Lock()
        self._cache: SystemConfig | None = None

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
            row = s.get(SystemRow, _ROW_ID)
            if row is not None:
                self._cache = SystemConfig.model_validate_json(row.data)
        if self._cache is None:
            path = self._resolve_path()
            if path and os.path.exists(path):
                self._cache = self._import_legacy(path)
            else:
                self._cache = SystemConfig()

    def _import_legacy(self, path: str) -> SystemConfig:
        """One-time, best-effort import of the legacy JSON file. Never
        destructive: the file is left in place (as a backup) regardless of
        outcome."""
        try:
            config = SystemConfig.model_validate_json(open(path).read())
        except Exception as exc:
            logger.warning(
                "legacy import: could not parse %s (%s); falling back to defaults, file left untouched",
                path, exc,
            )
            config = SystemConfig()
        else:
            logger.info("legacy import from %s: base_context imported (file kept as backup)", path)
        self._put(config)
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
            config = self._cache.model_copy(update={"base_context": value})
            self._put(config)
            self._cache = config
            return config

    def set_openrouter_api_key(self, value: str) -> SystemConfig:
        with self._lock:
            self._ensure()
            config = self._cache.model_copy(update={"openrouter_api_key": value})
            self._put(config)
            self._cache = config
            return config


system_config_store = SystemConfigStore(settings_attr="system_config_path")
