"""The fourth SqliteBackedStore. No legacy-JSON predecessor exists for plugins,
so `legacy_parse` is never reachable: with no `path` and no `settings_attr`,
_resolve_path() returns None and _ensure() skips the import branch entirely."""

from __future__ import annotations

from app.services.db.config_models import PluginRow
from app.services.db.config_store import SqliteBackedStore
from app.services.plugins.models import Plugin


def _no_legacy(path: str) -> dict[str, dict]:
    return {}


class PluginStore(SqliteBackedStore[Plugin]):
    def __init__(self, path: str | None = None) -> None:
        super().__init__(
            path,
            row_cls=PluginRow,
            model_cls=Plugin,
            key_attr="name",
            legacy_parse=_no_legacy,
        )


plugin_store = PluginStore()
