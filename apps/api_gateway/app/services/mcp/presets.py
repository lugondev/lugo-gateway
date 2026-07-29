from __future__ import annotations

from typing import TYPE_CHECKING

from app.services.mcp.models import McpServer

if TYPE_CHECKING:
    from app.services.mcp.server_store import McpServerStore

# Built-in MCP servers (see servers/mcp-basic-tools) shipped disabled by default —
# the URL is a placeholder until the user points it at their deployed instance.
PRESET_SERVERS: list[McpServer] = [
    McpServer(
        name="basic-tools",
        url="http://localhost:8090",
        headers={},
        enabled=False,
    ),
]

PRESET_NAMES: frozenset[str] = frozenset(p.name for p in PRESET_SERVERS)


def seed_default_servers(store: "McpServerStore") -> None:
    """Add preset servers that aren't already present. Never overwrites an
    existing entry, so user edits/deletes survive across restarts.

    Uses exists() rather than `get(preset.name) is None`: a preset row that
    fails to validate on load (e.g. a validator added after the row was
    written) still occupies preset.name and must not be silently replaced
    by fresh defaults -- that would be silent data loss on every restart
    (H4)."""
    for preset in PRESET_SERVERS:
        if not store.exists(preset.name):
            store.upsert(preset)
