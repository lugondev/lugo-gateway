from __future__ import annotations

import json

from app.services.db.config_models import ProfileRow
from app.services.db.config_store import SqliteBackedStore
from app.services.profiles.models import Profile


def _parse_legacy_raw(path: str) -> dict[str, dict]:
    return json.loads(open(path).read()).get("profiles", {})


class ProfileStore(SqliteBackedStore[Profile]):
    def __init__(self, path: str | None = None) -> None:
        super().__init__(
            path, row_cls=ProfileRow, model_cls=Profile,
            key_attr="name", legacy_parse=_parse_legacy_raw,
            settings_attr="profiles_path",
        )


profile_store = ProfileStore()
