from __future__ import annotations

import json
import threading
from pathlib import Path

from app.core.settings import settings
from app.services.mcp.models import McpServer


class McpServerStore:
    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._ensure()

    def _ensure(self) -> None:
        if not self._path.exists():
            self._write({})

    def _read(self) -> dict:
        try:
            data = json.loads(self._path.read_text())
            return data.get("servers", {})
        except (json.JSONDecodeError, FileNotFoundError):
            return {}

    def _write(self, servers: dict) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"servers": servers}, indent=2))
        tmp.replace(self._path)

    def list(self) -> dict[str, McpServer]:
        with self._lock:
            return {k: McpServer.model_validate(v) for k, v in self._read().items()}

    def get(self, name: str) -> McpServer | None:
        return self.list().get(name)

    def upsert(self, entry: McpServer) -> None:
        with self._lock:
            servers = self._read()
            servers[entry.name] = entry.model_dump()
            self._write(servers)

    def delete(self, name: str) -> None:
        with self._lock:
            servers = self._read()
            servers.pop(name, None)
            self._write(servers)


mcp_server_store = McpServerStore(settings.mcp_servers_path)
