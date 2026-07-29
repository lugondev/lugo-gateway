"""TTS against any OpenAI-compatible /audio/speech endpoint.

The name describes the protocol, not the backend -- the entry can point at
apps/model_service or any other compatible host. The entry is resolved per
call, so admin edits take effect without a provider rebuild.

Only WAV is handled: apps/model_service serves RenderingTTSProvider engines,
which all produce WAV.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from pathlib import Path

import httpx

from app.schemas.tts import TTSRequest
from app.services.http_errors import translate_httpx_error
from app.services.model_registry.store import model_registry_store
from app.services.providers.resolve import resolve_credentials
from app.services.tts.base import RenderingTTSProvider

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 60.0
# How long a fetched {"data": [...], "supports_clone": bool} capability schema
# stays valid before the next list_voices()/supports_voice_clone() call re-fetches
# it. Just long enough that a UI asking for both back-to-back (e.g. the TTS
# profile form's engine-change handler) shares one round trip, short enough
# that flipping the remote engine doesn't need a gateway restart to notice.
_CAPABILITIES_TTL_SECONDS = 30.0


def _looks_like_wav(data: bytes) -> bool:
    """Cheap RIFF/WAVE container sniff.

    Not a full parse -- just enough to catch a 200 response that's actually a
    JSON error page, an MP3, or a truncated body before it reaches the Opus
    hot path's wave.open() (core/audio.wav_bytes_to_pcm16), which raises a
    bare wave.Error from inside asyncio.to_thread -- outside render_wav's
    ProviderError wrapping.
    """
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def _read_ref_audio_base64(path: str) -> str:
    """Blocking read + base64 encode of a reference-audio clip. Run via
    ``asyncio.to_thread`` -- never call directly from a coroutine, see
    ``_render_wav``."""
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


class HttpTtsProvider(RenderingTTSProvider):
    name = "http_tts"
    sample_rate = 24000

    def __init__(
        self,
        name: str = "http_tts",
        timeout_seconds: float = _DEFAULT_TIMEOUT,
        entry: dict | None = None,
    ) -> None:
        self.name = name
        self.timeout_seconds = timeout_seconds
        # Only the registry's test-before-add call passes an entry.
        self._entry_override = entry
        # base_url -> (fetched_at, capabilities dict); see _CAPABILITIES_TTL_SECONDS.
        self._capabilities_cache: dict[str, tuple[float, dict]] = {}

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

    def available(self) -> bool:
        """Configured = some enabled registry row carries a base_url. Mirrors
        what STTService.list_engines() already computes for http_stt; sync
        because TTSService.list_engines() is sync."""
        row = model_registry_store.find_enabled_sync("tts", self.name)
        return bool((row or {}).get("base_url", "").strip())

    def install_hint(self) -> str:
        return "Add a Model Registry entry pointing at a TTS service base URL."

    async def _fetch_capabilities(self) -> dict:
        """GET {base_url}/voices -- the deployed engine's self-described schema
        of preset voices + clone support (see model_service's routes_tts.py).
        Cached briefly per base_url; degrades to "no voices, no clone" rather
        than raising, since this is a UI nicety, not part of synthesis itself."""
        entry = await self._resolve_entry()
        if entry:
            base_url, api_key = await resolve_credentials(entry)
        else:
            base_url, api_key = "", ""
        base_url = base_url.strip()
        empty = {"voices": [], "supports_clone": False}
        if not base_url:
            return empty

        cached = self._capabilities_cache.get(base_url)
        if cached is not None and (time.monotonic() - cached[0]) < _CAPABILITIES_TTL_SECONDS:
            return cached[1]

        api_key = api_key.strip()
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        timeout = (entry.get("config") or {}).get("timeout_seconds", self.timeout_seconds)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(f"{base_url.rstrip('/')}/voices", headers=headers)
                response.raise_for_status()
            body = response.json()
            result = {
                "voices": body.get("data") or [],
                "supports_clone": bool(body.get("supports_clone")),
            }
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("%s: failed to fetch remote voice capabilities: %s", self.name, exc)
            result = empty

        self._capabilities_cache[base_url] = (time.monotonic(), result)
        return result

    async def list_voices(self) -> list[dict]:
        return (await self._fetch_capabilities())["voices"]

    async def supports_voice_clone(self) -> bool:
        return (await self._fetch_capabilities())["supports_clone"]

    async def _render_wav(self, payload: TTSRequest) -> bytes:
        entry = await self._resolve_entry(payload.model_id)
        if entry:
            base_url, api_key = await resolve_credentials(entry)
        else:
            base_url, api_key = "", ""
        base_url = base_url.strip()
        if not base_url:
            raise RuntimeError(
                f"{self.name} is not configured. Add a Model Registry entry with the "
                f"service's base URL (e.g. http://tts-service:8100/v1)."
            )

        api_key = api_key.strip()
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
        if payload.ref_audio_path:
            # A gateway-local filesystem path means nothing on the far side of
            # this HTTP call -- send the actual bytes instead, the same way
            # model_service's /v1/voices tells us whether this engine even
            # supports cloning before we bother.
            #
            # Both the disk read and the base64 encode are blocking calls that
            # can run long enough (a large ref clip) to freeze the single
            # asyncio event loop for the whole gateway process -- offload them
            # to a worker thread, matching the asyncio.to_thread pattern the
            # other five providers already use for their blocking synthesis
            # work (see vieneu_provider._render_wav).
            body["ref_audio_base64"] = await asyncio.to_thread(
                _read_ref_audio_base64, payload.ref_audio_path
            )
            body["ref_text"] = payload.ref_text

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
