"""Detect the running container's capabilities (stdlib-only, never raises).

Used by the model recommender to decide which STT/TTS/LLM/VAD models actually fit
the machine the gateway runs on. Unknown values are ``None`` and are treated as
"unknown" by the recommender — only a definitively-failed hard requirement marks a
model incompatible.
"""

import os
import platform
import shutil
import sys
from dataclasses import asdict, dataclass

from app.core.deps import module_available
from app.services.system_config import system_config_store

# Optional Python modules probed for the recommender's `requires` flags.
_PROBE_MODULES = [
    "faster_whisper",
    "vosk",
    "vieneu",
    "silero_vad",
    "pyannote.audio",
    "mlx_whisper",
    "opuslib",
    "mlx_qwen3_asr",
    "qwen_asr",
]


@dataclass
class Capabilities:
    os: str | None
    arch: str | None
    apple_silicon: bool
    cpu_count: int | None
    ram_total_gb: float | None
    disk_free_gb: float | None
    mlx: bool
    cuda: bool
    libopus: bool
    ollama: bool
    modules: dict

    def has(self, flag: str) -> bool:
        """Resolve a candidate ``requires`` flag against named fields + modules."""
        named = {
            "apple_silicon": self.apple_silicon,
            "mlx": self.mlx,
            "cuda": self.cuda,
            "libopus": self.libopus,
            "ollama": self.ollama,
        }
        if flag in named:
            return bool(named[flag])
        return bool(self.modules.get(flag, False))

    def as_dict(self) -> dict:
        return asdict(self)


def _ram_total_gb() -> float | None:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return round(pages * page_size / (1024**3), 1)
    except (ValueError, OSError, AttributeError):
        pass
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return round(int(line.split()[1]) / (1024**2), 1)
    except Exception:  # noqa: BLE001
        return None
    return None


def _disk_free_gb(path: str) -> float | None:
    try:
        target = path if os.path.isdir(path) else "."
        return round(shutil.disk_usage(target).free / (1024**3), 1)
    except Exception:  # noqa: BLE001
        return None


def _cuda() -> bool:
    """Can the host actually run CUDA GPU compute?

    Authoritative when a GPU framework is already loaded (torch.cuda.is_available()
    is what qwen-asr / vieneu[gpu] actually rely on). Falls back to the NVIDIA driver
    (nvidia-smi or /proc) so a Colab T4 is detected even before torch is imported.
    Only NVIDIA-CUDA is considered — no project engine targets AMD/Intel GPUs, so
    detecting them would not enable anything.
    """
    torch = sys.modules.get("torch")  # use it only if already imported (avoid slow import)
    if torch is not None:
        try:
            if torch.cuda.is_available():
                return True
        except Exception:  # noqa: BLE001
            pass
    if shutil.which("nvidia-smi") is not None:
        return True
    return os.path.exists("/proc/driver/nvidia/version")


def _libopus() -> bool:
    try:
        from app.core.opus import opus_available

        return bool(opus_available())
    except Exception:  # noqa: BLE001
        return False


def _ollama() -> bool:
    try:
        ollama_bin = system_config_store.get().conversation_llm.ollama_bin
        if ollama_bin and os.path.exists(ollama_bin):
            return True
        return shutil.which("ollama") is not None or os.path.exists(
            "/opt/homebrew/opt/ollama/bin/ollama"
        )
    except Exception:  # noqa: BLE001
        return False


def detect_capabilities() -> Capabilities:
    try:
        sys_os = platform.system().lower()
    except Exception:  # noqa: BLE001
        sys_os = None
    try:
        arch = platform.machine().lower()
    except Exception:  # noqa: BLE001
        arch = None

    apple_silicon = sys_os == "darwin" and arch in {"arm64", "aarch64"}

    modules = {m: module_available(m) for m in _PROBE_MODULES}
    try:
        modules["omnivoice"] = os.path.isdir(system_config_store.get().omnivoice.omnivoice_path)
    except Exception:  # noqa: BLE001
        modules["omnivoice"] = False

    mlx = bool(modules.get("mlx_whisper"))

    return Capabilities(
        os=sys_os,
        arch=arch,
        apple_silicon=apple_silicon,
        cpu_count=os.cpu_count(),
        ram_total_gb=_ram_total_gb(),
        disk_free_gb=_disk_free_gb(system_config_store.get().stt_local.stt_model_dir),
        mlx=mlx,
        cuda=_cuda(),
        libopus=_libopus(),
        ollama=_ollama(),
        modules=modules,
    )
