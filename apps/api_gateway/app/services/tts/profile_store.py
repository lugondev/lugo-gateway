from __future__ import annotations

import json

from app.core.settings import settings
from app.services.db.config_models import TtsProfileRow
from app.services.db.config_store import SqliteBackedStore
from app.services.tts.profile_models import TtsProfile


def _parse_legacy(path: str) -> dict[str, TtsProfile]:
    data = json.loads(open(path).read()).get("profiles", {})
    return {k: TtsProfile.model_validate(v) for k, v in data.items()}


class TtsProfileStore(SqliteBackedStore[TtsProfile]):
    def __init__(self, path: str) -> None:
        super().__init__(
            path, row_cls=TtsProfileRow, model_cls=TtsProfile,
            key_attr="name", legacy_parse=_parse_legacy,
        )


tts_profile_store = TtsProfileStore(settings.tts_profiles_path)
