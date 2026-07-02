"""Conversation LLM (Ollama) model management.

Talks to a local Ollama daemon's native API (derived from
CONVERSATION_LLM_BASE_URL by stripping the trailing ``/v1``):
- list installed models      GET  /api/tags
- pull (download) a model     POST /api/pull   (streams progress)
- delete a model              DELETE /api/delete
Active selection is a runtime override on the conversation responder.
"""

import asyncio
import logging
import os
import re
import shutil
import subprocess

import httpx

from app.core.errors import AppError
from app.core.settings import settings
from app.services.conversation.responder import get_active_llm_model, set_active_llm_config

logger = logging.getLogger(__name__)


def _ollama_bin() -> str | None:
    candidates = [settings.ollama_bin, shutil.which("ollama"), "/opt/homebrew/opt/ollama/bin/ollama"]
    for c in candidates:
        if c and (shutil.which(c) or os.path.isfile(c)):
            return c
    return None

_MODEL_RE = re.compile(r"^[A-Za-z0-9._:\-/]+$")

LLM_SUGGESTIONS = [
    {"model": "gemma2:2b", "label": "Gemma 2 2B (~1.6 GB, fast)"},
    {"model": "gemma2:9b", "label": "Gemma 2 9B (~5.4 GB, balanced)"},
    {"model": "gemma2:27b", "label": "Gemma 2 27B (~16 GB, best quality)"},
]


def _ollama_base() -> str:
    base = settings.conversation_llm_base_url.rstrip("/")
    return base[:-3].rstrip("/") if base.endswith("/v1") else base


def _is_remote_endpoint() -> bool:
    """True when base_url points at a cloud API (not localhost/LAN Ollama)."""
    from urllib.parse import urlparse
    host = urlparse(settings.conversation_llm_base_url).hostname or ""
    return bool(host) and host not in ("localhost", "127.0.0.1", "0.0.0.0", "::1") \
        and not host.startswith("192.168.") and not host.endswith(".local")


class LlmManager:
    def __init__(self) -> None:
        self._jobs: dict[str, dict] = {}

    def validate(self, model: str) -> None:
        if not _MODEL_RE.match(model):
            raise AppError(f"Invalid model name: {model!r}")

    def available(self) -> bool:
        if _is_remote_endpoint():
            return bool(settings.conversation_llm_base_url and settings.conversation_llm_api_key)
        base = _ollama_base()
        if not base:
            return False
        try:
            return httpx.get(f"{base}/api/version", timeout=2.0).status_code == 200
        except httpx.HTTPError:
            return False

    def _installed(self) -> list[dict]:
        base = _ollama_base()
        try:
            resp = httpx.get(f"{base}/api/tags", timeout=3.0)
            resp.raise_for_status()
            models = resp.json().get("models", [])
        except httpx.HTTPError:
            return []
        return [{"model": m.get("name", ""), "size_bytes": m.get("size", 0)} for m in models]

    def _running_models(self) -> set[str]:
        """Models currently loaded in memory (Ollama /api/ps)."""
        base = _ollama_base()
        try:
            resp = httpx.get(f"{base}/api/ps", timeout=2.0)
            resp.raise_for_status()
            return {m.get("name", "") for m in resp.json().get("models", [])}
        except httpx.HTTPError:
            return set()

    def snapshot(self) -> dict:
        remote = _is_remote_endpoint()
        available = self.available()
        installed = self._installed() if (available and not remote) else []
        running = self._running_models() if (available and not remote) else set()
        names = {m["model"] for m in installed}
        active = get_active_llm_model()
        return {
            "available": available,
            "remote": remote,
            "base_url": settings.conversation_llm_base_url,
            "active": active,
            "running": active in running,
            "installed": [
                {**m, "active": m["model"] == active, "running": m["model"] in running}
                for m in installed
            ],
            "suggestions": [
                {
                    **s,
                    "installed": s["model"] in names,
                    "active": s["model"] == active,
                    "running": s["model"] in running,
                }
                for s in LLM_SUGGESTIONS
            ],
            "jobs": self._jobs,
        }

    async def download(self, model: str) -> None:
        self.validate(model)
        if self._jobs.get(model, {}).get("state") == "downloading":
            return
        self._jobs[model] = {"state": "downloading", "progress": 0.0, "status": "starting", "error": None}
        base = _ollama_base()
        if not base:
            self._jobs[model] = {
                "state": "error", "progress": 0.0, "status": "error",
                "error": "Ollama is not configured on this server. Local LLM download needs "
                         "Ollama running — set CONVERSATION_LLM_BASE_URL (e.g. "
                         "http://host:11434/v1) or use an online LLM instead.",
            }
            return
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("POST", f"{base}/api/pull", json={"name": model}) as resp:
                    if resp.status_code != 200:
                        raise AppError(f"Ollama pull failed (HTTP {resp.status_code})")
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        self._update_job(model, line)
            self._jobs[model] = {"state": "installed", "progress": 1.0, "status": "success", "error": None}
        except Exception as exc:  # noqa: BLE001 - report to UI
            self._jobs[model] = {"state": "error", "progress": 0.0, "status": "error", "error": str(exc)}

    def _update_job(self, model: str, line: str) -> None:
        import json

        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return
        job = self._jobs.get(model)
        if not job:
            return
        job["status"] = data.get("status", job["status"])
        total, completed = data.get("total"), data.get("completed")
        if total:
            job["progress"] = round(completed / total, 4) if completed else 0.0

    def select(self, model: str) -> None:
        self.validate(model)
        # Selecting a local model also pins the conversation to the Ollama endpoint,
        # overriding any stale online (e.g. OpenAI) base_url so the model and endpoint
        # can never mismatch (which surfaces as "model not found" errors).
        base = settings.conversation_llm_base_url or "http://localhost:11434/v1"
        set_active_llm_config(base, settings.conversation_llm_api_key, model)

    async def _is_up(self) -> bool:
        base = _ollama_base()
        if not base:
            return False
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                return (await client.get(f"{base}/api/version")).status_code == 200
        except httpx.HTTPError:
            return False

    async def _warm(self, model: str) -> bool:
        """Load the model into memory so the first conversation reply isn't slow."""
        base = _ollama_base()
        try:
            async with httpx.AsyncClient(timeout=180.0) as client:
                resp = await client.post(
                    f"{base}/api/generate",
                    json={"model": model, "prompt": "hi", "stream": False, "options": {"num_predict": 1}},
                )
                return resp.status_code == 200
        except httpx.HTTPError as exc:
            logger.warning("Ollama warm failed: %s", exc)
            return False

    async def start_service(self, warm: bool = True) -> dict:
        """Ensure the Ollama (OpenAI-compatible) service is running; optionally warm
        the active model. Spawns `ollama serve` detached when not reachable."""
        if not settings.conversation_llm_base_url:
            raise AppError("CONVERSATION_LLM_BASE_URL is not set")

        started = False
        if not await self._is_up():
            binary = _ollama_bin()
            if not binary:
                raise AppError("ollama binary not found — install Ollama (brew install ollama)")
            subprocess.Popen(  # noqa: S603 - launching the local ollama daemon
                [binary, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            started = True
            for _ in range(40):
                await asyncio.sleep(0.5)
                if await self._is_up():
                    break
            if not await self._is_up():
                raise AppError("Ollama did not become ready in time")

        active = get_active_llm_model()
        warmed = await self._warm(active) if warm else False
        return {
            "available": True,
            "started": started,
            "warmed": warmed,
            "active": active,
            "base_url": settings.conversation_llm_base_url,
        }

    async def stop(self) -> dict:
        """Stop the Ollama daemon so the LLM is truly offline until restarted.

        (Unlike unloading a model, this terminates `ollama serve`, so chat/
        conversation requests fail until Start brings it back.)
        """
        try:
            subprocess.run(["pkill", "-f", "ollama serve"], capture_output=True, check=False)
        except OSError as exc:
            raise AppError(f"Stop failed: {exc}") from exc
        for _ in range(20):
            await asyncio.sleep(0.3)
            if not await self._is_up():
                break
        return {"stopped": True, "available": await self._is_up()}

    async def delete(self, model: str) -> None:
        self.validate(model)
        base = _ollama_base()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.request("DELETE", f"{base}/api/delete", json={"name": model})
            if resp.status_code not in (200, 404):
                raise AppError(f"Ollama delete failed (HTTP {resp.status_code})")
        except httpx.HTTPError as exc:
            raise AppError(f"Ollama delete failed: {exc}") from exc
        self._jobs.pop(model, None)


llm_manager = LlmManager()
