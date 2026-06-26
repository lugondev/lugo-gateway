"""Whisper (faster-whisper) model management.

faster-whisper downloads weights from the Hugging Face hub (``Systran/faster-whisper-*``)
into ``~/.cache/huggingface/hub`` on first use. "Download" warms a size (triggering that
fetch); "delete" removes its hub cache directory; "select" switches the active size at
runtime. Download progress is indeterminate (the hub fetch is not instrumented here).
"""

import asyncio
import re
import shutil

from app.core.errors import AppError
from app.core.hf_cache import dir_size_bytes, hub_dir
from app.core.settings import settings
from app.services.stt.providers.whisper_provider import (
    get_active_whisper_model,
    set_active_whisper_model,
)

_SIZE_RE = re.compile(r"^[A-Za-z0-9.\-]+$")

# Common faster-whisper sizes (valid Systran/faster-whisper-* identifiers).
WHISPER_SIZES = [
    {"size": "tiny", "label": "Tiny (fastest)"},
    {"size": "base", "label": "Base"},
    {"size": "small", "label": "Small (default)"},
    {"size": "medium", "label": "Medium"},
    {"size": "large-v3", "label": "Large v3 (best quality)"},
]


class WhisperManager:
    def __init__(self) -> None:
        self._jobs: dict[str, dict] = {}

    def _cache_dirs(self, size: str) -> list:
        hub = hub_dir()
        if not hub.is_dir():
            return []
        # Match e.g. models--Systran--faster-whisper-large-v3 (exact suffix, no extra chars).
        return [p for p in hub.glob(f"models--*faster-whisper-{size}") if p.is_dir()]

    def _cached(self, size: str) -> bool:
        return bool(self._cache_dirs(size))

    def _size_bytes(self, size: str) -> int:
        return sum(dir_size_bytes(d) for d in self._cache_dirs(size))

    def validate(self, size: str) -> None:
        if not _SIZE_RE.match(size):
            raise AppError(f"Invalid whisper size: {size!r}")

    def snapshot(self) -> dict:
        active = get_active_whisper_model()
        models = []
        for entry in WHISPER_SIZES:
            size = entry["size"]
            cached = self._cached(size)
            models.append(
                {
                    **entry,
                    "cached": cached,
                    "active": size == active,
                    "size_bytes": self._size_bytes(size) if cached else 0,
                    "job": self._jobs.get(size),
                }
            )
        return {"models": models, "active": active}

    async def download(self, size: str) -> None:
        self.validate(size)
        if self._jobs.get(size, {}).get("state") == "downloading":
            return
        self._jobs[size] = {"state": "downloading", "error": None}
        try:
            await asyncio.to_thread(self._warm, size)
            self._jobs[size] = {"state": "installed", "error": None}
        except Exception as exc:  # noqa: BLE001 - report to UI
            self._jobs[size] = {"state": "error", "error": str(exc)}

    def _warm(self, size: str) -> None:
        from faster_whisper import WhisperModel

        WhisperModel(
            size,
            device=settings.whisper_local_device,
            compute_type=settings.whisper_local_compute_type,
        )

    def delete(self, size: str) -> None:
        self.validate(size)
        dirs = self._cache_dirs(size)
        if not dirs:
            raise AppError(f"Whisper model '{size}' is not cached")
        hub = hub_dir().resolve()
        for d in dirs:
            if hub not in d.resolve().parents:
                raise AppError("Refusing to delete outside the hub cache")
            shutil.rmtree(d)
        self._jobs.pop(size, None)

    def select(self, size: str) -> None:
        self.validate(size)
        set_active_whisper_model(size)


whisper_manager = WhisperManager()
