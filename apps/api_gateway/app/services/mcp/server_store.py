from __future__ import annotations

import json

from app.services.db.config_models import McpServerRow
from app.services.db.config_store import SqliteBackedStore
from app.services.mcp.models import McpServer


def _parse_legacy_raw(path: str) -> dict[str, dict]:
    return json.loads(open(path).read()).get("servers", {})


class McpServerStore(SqliteBackedStore[McpServer]):
    def __init__(self, path: str | None = None) -> None:
        super().__init__(
            path, row_cls=McpServerRow, model_cls=McpServer,
            key_attr="name", legacy_parse=_parse_legacy_raw,
            settings_attr="mcp_servers_path",
        )


mcp_server_store = McpServerStore()
