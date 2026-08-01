"""STT against any OpenAI-compatible /audio/transcriptions endpoint.

The name describes the protocol, not the backend: the same Model Registry
entry can point at apps/model_service, a faster-whisper-server, or any other
compatible host.

Unlike RemoteWhisperProvider (which caches base_url/api_key at construction and
needs stt_service.reinit_remote_providers() after every admin edit), this
resolves its entry per call, so an edited entry takes effect immediately.
"""

from __future__ import annotations

import httpx

from app.schemas.stt import STTResult
from app.services.http_errors import translate_httpx_error
from app.services.model_registry.store import model_registry_store
from app.services.remote_endpoint import resolve_remote_endpoint
from app.services.stt.base import STTProvider

_DEFAULT_TIMEOUT = 60.0


class HttpSttProvider(STTProvider):
    def __init__(
        self,
        name: str = "http_stt",
        timeout_seconds: float = _DEFAULT_TIMEOUT,
        entry: dict | None = None,
    ) -> None:
        self.name = name
        self.timeout_seconds = timeout_seconds
        # Only the registry's test-before-add call passes an entry: at that
        # point the row being validated does not exist yet.
        self._entry_override = entry

    async def _resolve_entry(self, model: str | None) -> dict | None:
        if self._entry_override is not None:
            return self._entry_override
        if model:
            return await model_registry_store.find(kind="stt", engine=self.name, model_id=model)
        return await model_registry_store.find_enabled(kind="stt", engine=self.name)

    async def transcribe_bytes(
        self, audio_bytes: bytes, language: str | None = None, model: str | None = None
    ) -> STTResult:
        entry = await self._resolve_entry(model)
        # Shared with http_tts_provider -- services/remote_endpoint.py.
        base_url, api_key, timeout = await resolve_remote_endpoint(
            entry, name=self.name, example_url="http://stt-service:8100/v1",
            default_timeout=self.timeout_seconds,
        )

        endpoint = f"{base_url.rstrip('/')}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        data = {"model": model or entry.get("model_id", ""), "response_format": "json"}
        if language:
            data["language"] = language
        files = {"file": ("audio.wav", audio_bytes, "audio/wav")}

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(endpoint, headers=headers, data=data, files=files)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            raise translate_httpx_error(self.name, exc) from exc

        return STTResult(
            engine=self.name, text=str(payload.get("text", "")).strip(), is_final=True, confidence=None
        )
