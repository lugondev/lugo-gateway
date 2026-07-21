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
from app.services.http_errors import translate_httpx_error
from app.services.model_registry.store import model_registry_store
from app.services.tts.base import RenderingTTSProvider

_DEFAULT_TIMEOUT = 60.0


def _looks_like_wav(data: bytes) -> bool:
    """Cheap RIFF/WAVE container sniff.

    Not a full parse -- just enough to catch a 200 response that's actually a
    JSON error page, an MP3, or a truncated body before it reaches the Opus
    hot path's wave.open() (core/audio.wav_bytes_to_pcm16), which raises a
    bare wave.Error from inside asyncio.to_thread -- outside render_wav's
    ProviderError wrapping.
    """
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


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

    async def _resolve_entry(self, model_id: str = "") -> dict | None:
        if self._entry_override is not None:
            return self._entry_override
        # A specific row was selected (engine|model_id from the registry
        # options): resolve it exactly so the choice is deterministic. Empty
        # model_id keeps the legacy first-enabled fallback for callers that
        # haven't been migrated to row-based selection yet.
        if model_id:
            return await model_registry_store.find(
                kind="tts", engine=self.name, model_id=model_id
            )
        return await model_registry_store.find_enabled(kind="tts", engine=self.name)

    def detail(self) -> str:
        return "OpenAI-compatible /audio/speech (per-registry-row service)"

    def install_hint(self) -> str:
        return "Add a Model Registry entry pointing at a TTS service base URL."

    async def _render_wav(self, payload: TTSRequest) -> bytes:
        entry = await self._resolve_entry(payload.model_id)
        base_url = (entry or {}).get("base_url", "").strip()
        if not base_url:
            raise RuntimeError(
                f"{self.name} is not configured. Add a Model Registry entry with the "
                f"service's base URL (e.g. http://tts-service:8100/v1)."
            )

        api_key = (entry or {}).get("api_key", "").strip()
        configured_timeout = (entry.get("config") or {}).get("timeout_seconds")
        timeout = configured_timeout if configured_timeout is not None else self.timeout_seconds

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
        except httpx.HTTPError as exc:
            raise translate_httpx_error(self.name, exc) from exc

        content = response.content
        if not _looks_like_wav(content):
            raise RuntimeError(
                f"{self.name} returned {len(content)} bytes that are not a WAV file "
                f"(expected a RIFF/WAVE header, got {content[:16]!r})"
            )
        return content
