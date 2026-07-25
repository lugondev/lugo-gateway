"""QwenCloud (Alibaba DashScope Model Studio, dashscope-intl) STT.

One engine, two model families selected by the registry entry's model:
  - qwen3-asr-flash: batch via inline multimodal-generation HTTP; realtime via
    the OpenAI-Realtime-compatible WebSocket (/api-ws/v1/realtime).
  - fun-asr:         batch via a one-shot native WS session; realtime via the
    DashScope-native run-task WebSocket (/api-ws/v1/inference).

Config resolves per-call from the Model Registry (like http_stt_provider), so
admin edits take effect immediately. See the design spec dated 2026-07-25.
"""

from __future__ import annotations

import base64
from urllib.parse import urlsplit

import httpx

from app.schemas.stt import STTResult
from app.services.http_errors import translate_httpx_error
from app.services.model_registry.store import model_registry_store
from app.services.providers.resolve import resolve_credentials
from app.services.stt.base import STTProvider

_DEFAULT_BASE_URL = "https://dashscope-intl.aliyuncs.com"
_DEFAULT_TIMEOUT = 60.0
_MM_PATH = "/api/v1/services/aigc/multimodal-generation/generation"


def _family(model: str | None) -> str:
    """Map a model id to its family. Default qwen3 (the primary family)."""
    return "funasr" if (model or "").strip().lower().startswith("fun-asr") else "qwen3"


def _host_base(base_url: str) -> str:
    """Scheme://host of a resolved base_url, dropping any path (e.g. a
    /compatible-mode/v1 suffix). QwenCloud STT endpoints are absolute from the
    host root. Falls back to the default host when empty/unparseable."""
    parts = urlsplit((base_url or "").strip() or _DEFAULT_BASE_URL)
    if not parts.scheme or not parts.netloc:
        return _DEFAULT_BASE_URL
    return f"{parts.scheme}://{parts.netloc}"


class QwenCloudSttProvider(STTProvider):
    name = "qwencloud"

    def __init__(self, name: str = "qwencloud", timeout_seconds: float = _DEFAULT_TIMEOUT,
                 entry: dict | None = None) -> None:
        self.name = name
        self.timeout_seconds = timeout_seconds
        self._entry_override = entry  # only the registry test-before-add call passes this

    async def _resolve_entry(self, model: str | None) -> dict | None:
        if self._entry_override is not None:
            return self._entry_override
        if model:
            return await model_registry_store.find(kind="stt", engine=self.name, model_id=model)
        return await model_registry_store.find_enabled(kind="stt", engine=self.name)

    async def _creds(self, model: str | None) -> tuple[dict, str, str, float]:
        entry = await self._resolve_entry(model)
        if entry:
            base_url, api_key = await resolve_credentials(entry)
        else:
            entry, base_url, api_key = {}, "", ""
        base_url = (base_url or "").strip() or (_DEFAULT_BASE_URL if entry else "")
        api_key = (api_key or "").strip()
        if not entry or not api_key:
            raise RuntimeError(
                f"{self.name} is not configured. Add a Model Registry entry with an API key "
                "(engine=qwencloud, model_id=qwen3-asr-flash or fun-asr)."
            )
        cfg_timeout = (entry.get("config") or {}).get("timeout_seconds")
        timeout = cfg_timeout if cfg_timeout is not None else self.timeout_seconds
        return entry, base_url, api_key, timeout

    async def transcribe_bytes(self, audio_bytes: bytes, language: str | None = None,
                               model: str | None = None) -> STTResult:
        entry, base_url, api_key, timeout = await self._creds(model)
        effective = model or entry.get("model_id") or "qwen3-asr-flash"
        # fun-asr has no inline HTTP endpoint -> one-shot WS (added in Task 4).
        return await self._qwen3_batch(base_url, api_key, timeout, effective, audio_bytes,
                                       language or (entry.get("config") or {}).get("language"))

    async def _qwen3_batch(self, base_url, api_key, timeout, model, audio_bytes, language):
        b64 = base64.b64encode(audio_bytes).decode("ascii")
        asr_options: dict = {"enable_lid": True}
        if language:
            asr_options["language"] = language
        body = {
            "model": model,
            "input": {"messages": [{"role": "user",
                "content": [{"audio": f"data:audio/wav;base64,{b64}"}]}]},
            "parameters": {"asr_options": asr_options},
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(f"{_host_base(base_url)}{_MM_PATH}",
                                         headers=headers, json=body)
                resp.raise_for_status()
                payload = resp.json()
        except httpx.HTTPError as exc:
            raise translate_httpx_error(self.name, exc) from exc
        return STTResult(engine=self.name, text=_mm_text(payload), is_final=True, confidence=None)


def _mm_text(payload: dict) -> str:
    """Pull transcript from output.choices[0].message.content[0].text (defensive)."""
    try:
        content = payload["output"]["choices"][0]["message"]["content"]
        for part in content:
            if isinstance(part, dict) and "text" in part:
                return str(part["text"]).strip()
    except (KeyError, IndexError, TypeError):
        pass
    return ""
