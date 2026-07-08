from __future__ import annotations

import json

from app.core.settings import settings
from app.services.db.config_models import ProfileRow
from app.services.db.config_store import SqliteBackedStore
from app.services.profiles.models import Profile


def _parse_legacy(path: str) -> dict[str, Profile]:
    data = json.loads(open(path).read()).get("profiles", {})
    return {k: Profile.model_validate(v) for k, v in data.items()}


class ProfileStore(SqliteBackedStore[Profile]):
    def __init__(self, path: str) -> None:
        super().__init__(
            path, row_cls=ProfileRow, model_cls=Profile,
            key_attr="name", legacy_parse=_parse_legacy,
        )


profile_store = ProfileStore(settings.profiles_path)
