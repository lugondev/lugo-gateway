"""Runtime pip-install of optional engine packages — gated + allowlist-only.

Lets the Models-tab panel offer a one-click "Install" for engines that just need a
Python package (vieneu, qwen-asr, ...). Guarded two ways:
  1. Disabled unless ``settings.allow_runtime_install`` is true (keep OFF on public
     deploys — this runs pip on the server).
  2. Only packages in the fixed allowlist below can be installed (no arbitrary input).
"""

import asyncio
import importlib
import sys

from app.core.errors import AppError, RuntimeInstallDisabledError
from app.core.settings import settings

# requirement flag (as used in the recommend catalog) -> exact pip spec to install.
ALLOWLIST: dict[str, str] = {
    "vieneu": "vieneu",
    "qwen_asr": "qwen-asr",
    "mlx_qwen3_asr": "mlx-qwen3-asr",
    "silero_vad": "silero-vad",
    "pyannote.audio": "pyannote.audio",
}


class InstallManager:
    def __init__(self) -> None:
        self._jobs: dict[str, dict] = {}

    def validate(self, package: str) -> str:
        """Check the endpoint is enabled and the package is allowed; return pip spec."""
        if not settings.allow_runtime_install:
            raise RuntimeInstallDisabledError(
                "Runtime install is disabled. Set ALLOW_RUNTIME_INSTALL=true to enable "
                "(keep it off on public deployments)."
            )
        spec = ALLOWLIST.get(package)
        if not spec:
            raise AppError(f"Package not in install allowlist: {package!r}")
        return spec

    def snapshot(self) -> dict:
        return {"enabled": settings.allow_runtime_install, "jobs": self._jobs}

    async def install(self, package: str) -> None:
        spec = self.validate(package)
        if self._jobs.get(package, {}).get("state") == "installing":
            return
        self._jobs[package] = {"state": "installing", "error": None}
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pip", "install", "-q", spec,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await proc.communicate()
            if proc.returncode != 0:
                self._jobs[package] = {"state": "error", "error": (out or b"").decode()[-600:]}
                return
            # Let find_spec/import see the freshly-installed package without a restart.
            importlib.invalidate_caches()
            self._jobs[package] = {"state": "installed", "error": None}
        except Exception as exc:  # noqa: BLE001 - report to the UI
            self._jobs[package] = {"state": "error", "error": str(exc)}


install_manager = InstallManager()
