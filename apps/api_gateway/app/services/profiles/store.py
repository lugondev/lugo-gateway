from __future__ import annotations

import json
import threading
from pathlib import Path

from app.core.settings import settings
from app.services.profiles.models import Profile


class ProfileStore:
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
            return data.get("profiles", {})
        except (json.JSONDecodeError, FileNotFoundError):
            return {}

    def _write(self, profiles: dict) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"profiles": profiles}, indent=2))
        tmp.replace(self._path)

    def list(self) -> dict[str, Profile]:
        with self._lock:
            return {k: Profile.model_validate(v) for k, v in self._read().items()}

    def get(self, name: str) -> Profile | None:
        return self.list().get(name)

    def upsert(self, profile: Profile) -> None:
        with self._lock:
            profiles = self._read()
            profiles[profile.name] = profile.model_dump()
            self._write(profiles)

    def delete(self, name: str) -> None:
        with self._lock:
            profiles = self._read()
            profiles.pop(name, None)
            self._write(profiles)


profile_store = ProfileStore(settings.profiles_path)
