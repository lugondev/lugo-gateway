"""Model manager: list / download / delete local Vosk models.

Vosk publishes models at ``{VOSK_MODEL_BASE_URL}/{name}.zip`` (see
https://alphacephei.com/vosk/models for the full list). Downloads are name-based
so the catalog is not hardcoded; a short suggestions list seeds the UI.

Whisper and OmniVoice weights are managed by their own libraries' caches, so they
are reported read-only by the status endpoint rather than file-managed here.
"""

import asyncio
import re
import shutil
import zipfile
from pathlib import Path

import httpx

from app.core.errors import AppError
from app.core.hf_cache import dir_size_bytes
from app.core.settings import settings

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Verified-existing starter suggestions (HTTP 200 at the Vosk model base URL).
# The UI also supports downloading any model name from alphacephei.com/vosk/models.
VOSK_SUGGESTIONS = [
    {"name": "vosk-model-small-en-us-0.15", "label": "English (small)"},
    {"name": "vosk-model-en-us-0.22", "label": "English (large)"},
    {"name": "vosk-model-small-vn-0.4", "label": "Vietnamese (small)"},
    {"name": "vosk-model-vn-0.4", "label": "Vietnamese (large)"},
]


class ModelManager:
    def __init__(self) -> None:
        self._base = Path(settings.stt_model_dir)
        self._base.mkdir(parents=True, exist_ok=True)
        # name -> {"state": downloading|installed|error, "progress": float, "error": str|None}
        self._jobs: dict[str, dict] = {}

    # ---- helpers ----
    def validate(self, name: str) -> None:
        if not _NAME_RE.match(name):
            raise AppError(f"Invalid model name: {name!r}")

    def _resolved_dir(self, name: str) -> Path:
        target = (self._base / name).resolve()
        if self._base.resolve() not in target.parents and target != self._base.resolve():
            raise AppError("Path traversal rejected")
        return target

    # ---- queries ----
    def list_installed(self) -> list[dict]:
        installed = []
        for child in sorted(self._base.iterdir()):
            if child.is_dir():
                installed.append(
                    {
                        "name": child.name,
                        "size_bytes": dir_size_bytes(child),
                        "path": str(child),
                    }
                )
        return installed

    def active_name(self) -> str:
        from app.services.stt.providers.vosk_provider import get_active_vosk_path

        return Path(get_active_vosk_path()).name

    def snapshot(self) -> dict:
        installed = self.list_installed()
        installed_names = {m["name"] for m in installed}
        active = self.active_name()
        return {
            "installed": [{**m, "active": m["name"] == active} for m in installed],
            "suggestions": [
                {**s, "installed": s["name"] in installed_names, "active": s["name"] == active}
                for s in VOSK_SUGGESTIONS
            ],
            "active": active,
            "jobs": self._jobs,
            "base_dir": str(self._base),
        }

    # ---- mutations ----
    async def download(self, name: str) -> None:
        self.validate(name)
        if self._jobs.get(name, {}).get("state") == "downloading":
            return
        self._jobs[name] = {"state": "downloading", "progress": 0.0, "error": None}
        try:
            url = f"{settings.vosk_model_base_url.rstrip('/')}/{name}.zip"
            zip_path = self._base / f"{name}.zip.part"
            async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
                async with client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        raise AppError(f"Model '{name}' not found (HTTP {resp.status_code})")
                    total = int(resp.headers.get("content-length", 0))
                    received = 0
                    with open(zip_path, "wb") as fh:
                        async for chunk in resp.aiter_bytes(65536):
                            fh.write(chunk)
                            received += len(chunk)
                            if total:
                                self._jobs[name]["progress"] = round(received / total, 4)
            await asyncio.to_thread(self._extract, zip_path)
            zip_path.unlink(missing_ok=True)
            self._jobs[name] = {"state": "installed", "progress": 1.0, "error": None}
        except Exception as exc:  # noqa: BLE001 - report to UI
            (self._base / f"{name}.zip.part").unlink(missing_ok=True)
            self._jobs[name] = {"state": "error", "progress": 0.0, "error": str(exc)}

    def _extract(self, zip_path: Path) -> None:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(self._base)

    def delete(self, name: str) -> None:
        self.validate(name)
        target = self._resolved_dir(name)
        if not target.is_dir():
            raise AppError(f"Model '{name}' is not installed")
        shutil.rmtree(target)
        self._jobs.pop(name, None)

    def select(self, name: str) -> None:
        from app.services.stt.providers.vosk_provider import set_active_vosk_path

        self.validate(name)
        target = self._resolved_dir(name)
        if not target.is_dir():
            raise AppError(f"Model '{name}' is not installed")
        set_active_vosk_path(str(target))


model_manager = ModelManager()
