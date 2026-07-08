from __future__ import annotations

import json

from app.services.db.config_models import TtsProfileRow
from app.services.db.config_store import SqliteBackedStore
from app.services.tts.profile_models import TtsProfile


def _parse_legacy_raw(path: str) -> dict[str, dict]:
    return json.loads(open(path).read()).get("profiles", {})


class TtsProfileStore(SqliteBackedStore[TtsProfile]):
    def __init__(self, path: str | None = None) -> None:
        super().__init__(
            path, row_cls=TtsProfileRow, model_cls=TtsProfile,
            key_attr="name", legacy_parse=_parse_legacy_raw,
            settings_attr="tts_profiles_path",
        )


tts_profile_store = TtsProfileStore()
