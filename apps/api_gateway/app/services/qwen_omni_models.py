"""Qwen3-Omni (audio-native STT) model management — MLX builds from the HF hub.

Download/select/delete the MLX-quantized Qwen3-Omni weights used by the `qwen_omni`
STT engine. Mirrors the whisper manager: download warms the hub cache, delete
removes it, select switches the active model at runtime.
"""

import asyncio
import re
import shutil

from app.core.errors import AppError
from app.core.hf_cache import dir_size_bytes, hub_dir
from app.services.stt.providers.qwen_omni_provider import (
    get_active_qwen_omni_model,
    set_active_qwen_omni_model,
)

_ID_RE = re.compile(r"^[A-Za-z0-9._/\-]+$")

# Only Qwen3-Omni currently has MLX builds that mlx-vlm can run (qwen3_omni_moe).
# Qwen2.5-Omni MLX weights exist but lack an mlx-vlm loader; Qwen3.5-Omni has no
# audio-omni MLX build yet — add here when they become available.
QWEN_OMNI_MODELS = [
    {"model": "mlx-community/Qwen3-Omni-30B-A3B-Instruct-4bit", "label": "Qwen3-Omni 30B · 4-bit (~16 GB, recommended)"},
    {"model": "mlx-community/Qwen3-Omni-30B-A3B-Instruct-6bit", "label": "Qwen3-Omni 30B · 6-bit (~24 GB)"},
    {"model": "mlx-community/Qwen3-Omni-30B-A3B-Instruct-8bit", "label": "Qwen3-Omni 30B · 8-bit (~32 GB)"},
]


def _mlx_available() -> bool:
    try:
        import mlx_vlm  # noqa: F401
    except ImportError:
        return False
    return True


class QwenOmniManager:
    def __init__(self) -> None:
        self._jobs: dict[str, dict] = {}

    def validate(self, model: str) -> None:
        if not _ID_RE.match(model) or model not in {m["model"] for m in QWEN_OMNI_MODELS}:
            raise AppError(f"Unknown Qwen-Omni model: {model!r}")

    def _cache_dir(self, model: str):
        hub = hub_dir()
        d = hub / f"models--{model.replace('/', '--')}"
        return d if d.is_dir() else None

    def _cached(self, model: str) -> bool:
        d = self._cache_dir(model)
        if not d:
            return False
        blobs = d / "blobs"
        # A partial download leaves *.incomplete blobs — don't report it as ready.
        if blobs.is_dir() and any(blobs.glob("*.incomplete")):
            return False
        return True

    def snapshot(self) -> dict:
        active = get_active_qwen_omni_model()
        mlx = _mlx_available()
        models = []
        for entry in QWEN_OMNI_MODELS:
            mid = entry["model"]
            cached = self._cached(mid)
            d = self._cache_dir(mid)
            models.append(
                {
                    **entry,
                    "cached": cached,
                    "active": mid == active,
                    "size_bytes": dir_size_bytes(d) if d else 0,
                    "job": self._jobs.get(mid),
                }
            )
        return {"available": mlx, "active": active, "models": models}

    async def download(self, model: str) -> None:
        self.validate(model)
        if not _mlx_available():
            raise AppError("mlx-vlm not installed — pip install -e '.[mlx]' (Apple Silicon only)")
        if self._jobs.get(model, {}).get("state") == "downloading":
            return
        self._jobs[model] = {"state": "downloading", "error": None}
        try:
            await asyncio.to_thread(self._fetch, model)
            self._jobs[model] = {"state": "installed", "error": None}
        except Exception as exc:  # noqa: BLE001 - surface to UI
            self._jobs[model] = {"state": "error", "error": str(exc)}

    def _fetch(self, model: str) -> None:
        from huggingface_hub import snapshot_download

        snapshot_download(model)

    def delete(self, model: str) -> None:
        self.validate(model)
        d = self._cache_dir(model)
        if not d:
            raise AppError(f"Model '{model}' is not cached")
        hub = hub_dir().resolve()
        if hub not in d.resolve().parents:
            raise AppError("Refusing to delete outside the hub cache")
        shutil.rmtree(d)
        self._jobs.pop(model, None)

    def select(self, model: str) -> None:
        self.validate(model)
        set_active_qwen_omni_model(model)


qwen_omni_manager = QwenOmniManager()
