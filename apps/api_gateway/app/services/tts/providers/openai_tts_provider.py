"""TTS against any OpenAI-compatible /audio/speech endpoint.

The name describes the protocol, not the backend -- the entry can point at
apps/model_service or any other compatible host. The entry is resolved per
call, so admin edits take effect without a provider rebuild.

Only WAV is handled: apps/model_service serves RenderingTTSProvider engines,
which all produce WAV.
"""

from __future__ import annotations

import httpx

from app.schemas.tts import TTSRequest
from app.services.model_registry.store import model_registry_store
from app.services.tts.base import RenderingTTSProvider

_DEFAULT_TIMEOUT = 60.0


class OpenAICompatTTSProvider(RenderingTTSProvider):
    name = "openai_tts"
    sample_rate = 24000

    def __init__(
        self,
        name: str = "openai_tts",
        timeout_seconds: float = _DEFAULT_TIMEOUT,
        entry: dict | None = None,
    ) -> None:
        self.name = name
        self.timeout_seconds = timeout_seconds
        # Only the registry's test-before-add call passes an entry.
        self._entry_override = entry

    async def _resolve_entry(self) -> dict | None:
        if self._entry_override is not None:
            return self._entry_override
        return await model_registry_store.find_enabled(kind="tts", engine=self.name)

    def detail(self) -> str:
        return self.name

    def install_hint(self) -> str:
        return "Add a Model Registry entry pointing at a TTS service base URL."

    async def _render_wav(self, payload: TTSRequest) -> bytes:
        entry = await self._resolve_entry()
        base_url = (entry or {}).get("base_url", "").strip()
        if not base_url:
            raise RuntimeError(
                f"{self.name} is not configured. Add a Model Registry entry with the "
                f"service's base URL (e.g. http://tts-service:8100/v1)."
            )

        api_key = (entry or {}).get("api_key", "").strip()
        timeout = (entry.get("config") or {}).get("timeout_seconds") or self.timeout_seconds

        endpoint = f"{base_url.rstrip('/')}/audio/speech"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        body = {
            "model": entry.get("model_id", ""),
            "input": payload.text,
            "voice": payload.voice,
            "speed": payload.speed,
            "language": payload.language,
            "response_format": "wav",
        }

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(endpoint, headers=headers, json=body)
                response.raise_for_status()
                return response.content
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"{self.name} returned HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"{self.name} request failed: {exc}") from exc
