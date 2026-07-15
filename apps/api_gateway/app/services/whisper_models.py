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
from app.services.stt.providers.whisper_provider import (
    PHOWHISPER_REPO,
    PHOWHISPER_SUBFOLDERS,
    get_active_whisper_model,
    is_phowhisper,
    resolve_whisper_model,
    set_active_whisper_model,
)
from app.services.model_registry.resolve import resolve_stt_local_device

_SIZE_RE = re.compile(r"^[A-Za-z0-9.\-]+$")

# Selectable whisper models. Standard sizes resolve to Systran/faster-whisper-*;
# the phowhisper-* ids are VinAI's Vietnamese fine-tune (CT2), best for Vietnamese.
WHISPER_SIZES = [
    {"size": "phowhisper-medium", "label": "PhoWhisper Medium — Vietnamese ⭐ (recommended)"},
    {"size": "phowhisper-large", "label": "PhoWhisper Large — Vietnamese (best, slower)"},
    {"size": "phowhisper-small", "label": "PhoWhisper Small — Vietnamese (fastest)"},
    {"size": "tiny", "label": "Tiny (fastest, multilingual)"},
    {"size": "base", "label": "Base (multilingual)"},
    {"size": "small", "label": "Small (multilingual)"},
    {"size": "medium", "label": "Medium (multilingual)"},
    {"size": "large-v3", "label": "Large v3 (multilingual, best)"},
    {"size": "large-v3-turbo", "label": "Large v3 Turbo (multilingual, ~6x faster)"},
]

_VALID_SIZES = {entry["size"] for entry in WHISPER_SIZES}


class WhisperManager:
    def __init__(self) -> None:
        self._jobs: dict[str, dict] = {}

    def _cache_dirs(self, size: str) -> list:
        hub = hub_dir()
        if not hub.is_dir():
            return []
        if is_phowhisper(size):
            # All PhoWhisper sizes share one repo dir; a size is "cached" when its
            # subfolder snapshot has the model weights present.
            sub = PHOWHISPER_SUBFOLDERS[size]
            repo = hub / f"models--{PHOWHISPER_REPO.replace('/', '--')}"
            if not repo.is_dir():
                return []
            return [p for p in repo.glob(f"snapshots/*/{sub}") if (p / "model.bin").exists()]
        # Match e.g. models--Systran--faster-whisper-large-v3 (exact suffix, no extra chars).
        return [p for p in hub.glob(f"models--*faster-whisper-{size}") if p.is_dir()]

    def _cached(self, size: str) -> bool:
        return bool(self._cache_dirs(size))

    def _size_bytes(self, size: str) -> int:
        return sum(dir_size_bytes(d) for d in self._cache_dirs(size))

    def validate(self, size: str) -> None:
        if not _SIZE_RE.match(size):
            raise AppError(f"Invalid whisper size: {size!r}")
        if size not in _VALID_SIZES:
            raise AppError(f"Unknown whisper size: {size!r}")

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

    def list_models(self) -> list[dict]:
        """Common STT model-registry shape (see app.services.stt.model_registry)."""
        snap = self.snapshot()
        return [
            {"id": m["size"], "label": m["label"], "cached": m["cached"], "active": m["active"]}
            for m in snap["models"]
        ]

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

        # resolve_whisper_model downloads the PhoWhisper subfolder (or passes a
        # standard size through) and returns a path faster-whisper can load.
        device_cfg = resolve_stt_local_device("whisper_local")
        WhisperModel(
            resolve_whisper_model(size),
            device=device_cfg["device"] or "cpu",
            compute_type=device_cfg["compute_type"],
        )

    def delete(self, size: str) -> None:
        self.validate(size)
        if not self._cached(size):
            raise AppError(f"Whisper model '{size}' is not cached")
        hub = hub_dir().resolve()
        if is_phowhisper(size):
            # Sizes share one repo (blobs are deduplicated), so removing the repo
            # dir clears every cached PhoWhisper size at once.
            repo = hub_dir() / f"models--{PHOWHISPER_REPO.replace('/', '--')}"
            if repo.is_dir() and hub in repo.resolve().parents:
                shutil.rmtree(repo)
        else:
            for d in self._cache_dirs(size):
                if hub not in d.resolve().parents:
                    raise AppError("Refusing to delete outside the hub cache")
                shutil.rmtree(d)
        self._jobs.pop(size, None)

    def select(self, size: str) -> None:
        self.validate(size)
        set_active_whisper_model(size)


whisper_manager = WhisperManager()
