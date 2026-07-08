from __future__ import annotations

import json

from app.core.settings import settings
from app.services.db.config_models import McpServerRow
from app.services.db.config_store import SqliteBackedStore
from app.services.mcp.models import McpServer
from app.services.mcp.presets import seed_default_servers


def _parse_legacy(path: str) -> dict[str, McpServer]:
    data = json.loads(open(path).read()).get("servers", {})
    return {k: McpServer.model_validate(v) for k, v in data.items()}


class McpServerStore(SqliteBackedStore[McpServer]):
    def __init__(self, path: str) -> None:
        super().__init__(
            path, row_cls=McpServerRow, model_cls=McpServer,
            key_attr="name", legacy_parse=_parse_legacy,
        )


mcp_server_store = McpServerStore(settings.mcp_servers_path)
seed_default_servers(mcp_server_store)
