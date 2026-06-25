"""TTS model management for OmniVoice and VieNeu.

Both engines download weights from the Hugging Face hub (shared
``~/.cache/huggingface/hub``), so download/cache/delete go through
``huggingface_hub`` in-process. Active selection is a runtime override on each
provider (reset on restart).

- OmniVoice: selectable by HF repo id (the CLI ``--model``); name-based, like Vosk.
- VieNeu: selectable by mode (v3turbo on CPU; others need ``vieneu[gpu]``).
"""

import asyncio
import logging
import re
import shutil
from pathlib import Path

from app.core.errors import AppError
from app.services.tts.providers.omnivoice_provider import (
    get_active_omnivoice_model,
    set_active_omnivoice_model,
)
from app.services.tts.providers.vieneu_provider import (
    get_active_vieneu_mode,
    set_active_vieneu_mode,
)

logger = logging.getLogger(__name__)

_REPO_RE = re.compile(r"^[A-Za-z0-9._\-/]+$")
_MODE_RE = re.compile(r"^[A-Za-z0-9._\-]+$")

OMNIVOICE_MODELS = [
    {"id": "k2-fsa/OmniVoice", "label": "OmniVoice (multilingual, 24kHz)"},
]

VIENEU_MODES = [
    {"mode": "v3turbo", "repo": "pnnbao-ump/VieNeu-TTS-v3-Turbo", "label": "v3 Turbo (48kHz, CPU)", "cpu": True},
    {"mode": "standard", "repo": None, "label": "Standard / GGUF (needs vieneu[gpu])", "cpu": False},
    {"mode": "turbo", "repo": None, "label": "Turbo (needs vieneu[gpu])", "cpu": False},
    {"mode": "fast", "repo": None, "label": "Fast / GPU (needs vieneu[gpu])", "cpu": False},
]


def _hub_dir() -> Path:
    return Path.home() / ".cache" / "huggingface" / "hub"


def _repo_dir(repo: str) -> Path:
    return _hub_dir() / f"models--{repo.replace('/', '--')}"


def _cached(repo: str | None) -> bool:
    return bool(repo) and _repo_dir(repo).is_dir()


def _size_bytes(repo: str | None) -> int:
    if not _cached(repo):
        return 0
    return sum(f.stat().st_size for f in _repo_dir(repo).rglob("*") if f.is_file())


class TtsModelManager:
    def __init__(self) -> None:
        # key -> {state, error}; key is repo (omnivoice) or mode (vieneu)
        self._jobs: dict[str, dict] = {}

    # ---------- snapshot ----------
    def snapshot(self) -> dict:
        active_omni = get_active_omnivoice_model()
        omni_models = [
            {
                **m,
                "cached": _cached(m["id"]),
                "active": m["id"] == active_omni,
                "size_bytes": _size_bytes(m["id"]),
                "job": self._jobs.get(m["id"]),
            }
            for m in OMNIVOICE_MODELS
        ]

        active_mode = get_active_vieneu_mode()
        vieneu_modes = [
            {
                **m,
                "cached": _cached(m["repo"]),
                "active": m["mode"] == active_mode,
                "size_bytes": _size_bytes(m["repo"]),
                "job": self._jobs.get(m["mode"]),
            }
            for m in VIENEU_MODES
        ]

        return {
            "omnivoice": {"active": active_omni, "models": omni_models},
            "vieneu": {"active": active_mode, "modes": vieneu_modes},
        }

    # ---------- OmniVoice (HF repo id) ----------
    def validate_repo(self, repo: str) -> None:
        if not _REPO_RE.match(repo) or "/" not in repo:
            raise AppError(f"Invalid model repo id: {repo!r}")

    async def download_omnivoice(self, repo: str) -> None:
        self.validate_repo(repo)
        if self._jobs.get(repo, {}).get("state") == "downloading":
            return
        self._jobs[repo] = {"state": "downloading", "error": None}
        try:
            await asyncio.to_thread(self._snapshot, repo)
            self._jobs[repo] = {"state": "installed", "error": None}
        except Exception as exc:  # noqa: BLE001
            self._jobs[repo] = {"state": "error", "error": str(exc)}

    def select_omnivoice(self, repo: str) -> None:
        self.validate_repo(repo)
        set_active_omnivoice_model(repo)

    def delete_omnivoice(self, repo: str) -> None:
        self.validate_repo(repo)
        self._delete_repo(repo)

    # ---------- VieNeu (mode) ----------
    def validate_mode(self, mode: str) -> None:
        if not _MODE_RE.match(mode):
            raise AppError(f"Invalid VieNeu mode: {mode!r}")

    async def download_vieneu(self, mode: str) -> None:
        self.validate_mode(mode)
        if self._jobs.get(mode, {}).get("state") == "downloading":
            return
        self._jobs[mode] = {"state": "downloading", "error": None}
        try:
            await asyncio.to_thread(self._warm_vieneu, mode)
            self._jobs[mode] = {"state": "installed", "error": None}
        except Exception as exc:  # noqa: BLE001
            self._jobs[mode] = {"state": "error", "error": str(exc)}

    def select_vieneu(self, mode: str) -> None:
        self.validate_mode(mode)
        set_active_vieneu_mode(mode)

    def delete_vieneu(self, mode: str) -> None:
        self.validate_mode(mode)
        repo = next((m["repo"] for m in VIENEU_MODES if m["mode"] == mode), None)
        if not repo:
            raise AppError(f"No deletable cache mapped for VieNeu mode '{mode}'")
        self._delete_repo(repo)

    # ---------- helpers ----------
    def _snapshot(self, repo: str) -> None:
        from huggingface_hub import snapshot_download

        snapshot_download(repo)

    def _warm_vieneu(self, mode: str) -> None:
        from vieneu import Vieneu

        Vieneu(mode=mode)

    def _delete_repo(self, repo: str) -> None:
        target = _repo_dir(repo)
        if not target.is_dir():
            raise AppError(f"Model '{repo}' is not cached")
        hub = _hub_dir().resolve()
        if hub not in target.resolve().parents:
            raise AppError("Refusing to delete outside the hub cache")
        shutil.rmtree(target)
        self._jobs.pop(repo, None)


tts_model_manager = TtsModelManager()
