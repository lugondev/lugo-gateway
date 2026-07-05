from __future__ import annotations

import json
import threading
from pathlib import Path

from pydantic import BaseModel

from app.core.settings import settings


class SystemConfig(BaseModel):
    base_context: str = ""


class SystemConfigStore:
    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._ensure()

    def _ensure(self) -> None:
        if not self._path.exists():
            self._write(SystemConfig())

    def _read(self) -> SystemConfig:
        try:
            return SystemConfig.model_validate_json(self._path.read_text())
        except (json.JSONDecodeError, FileNotFoundError, ValueError):
            return SystemConfig()

    def _write(self, config: SystemConfig) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(config.model_dump_json(indent=2))
        tmp.replace(self._path)

    def get(self) -> SystemConfig:
        with self._lock:
            return self._read()

    def set_base_context(self, value: str) -> SystemConfig:
        with self._lock:
            config = SystemConfig(base_context=value)
            self._write(config)
            return config


system_config_store = SystemConfigStore(settings.system_config_path)
