"""edge-tts — free cloud TTS via Microsoft Edge's Read Aloud service.

No API key, no local model; needs outbound network access. Unofficial
(reverse-engineered) API — test-UI/batch synthesis only, not the live
conversation pipeline (see docs/superpowers/specs/2026-07-11-edge-tts-provider-design.md
for why: the live path needs real WAV/PCM, this engine's native output is MP3).
"""

import asyncio

from app.core.deps import module_available
from app.core.errors import ProviderError
from app.schemas.tts import TTSRequest
from app.services.tts.base import TTSProvider

_SAMPLE_RATE = 24000
_MAX_ATTEMPTS = 3  # edge-tts's unofficial API intermittently drops the audio stream; retry before failing
# Back-to-back retries with no gap were observed to fail in a row (likely brief
# throttling); a short pause between attempts made the very next call succeed.
_RETRY_DELAY_SECONDS = 0.6


class EdgeTTSProvider(TTSProvider):
    name = "edge_tts"
    install_package = "edge_tts"
    sample_rate = _SAMPLE_RATE

    DEFAULT_VOICE = "vi-VN-HoaiMyNeural"
    VOICES = [
        {"label": "Hoài My (nữ)", "voice": "vi-VN-HoaiMyNeural"},
        {"label": "Nam Minh (nam)", "voice": "vi-VN-NamMinhNeural"},
    ]

    def available(self) -> bool:
        return module_available("edge_tts")

    def detail(self) -> str:
        return "Microsoft Edge TTS (cloud, no API key, network required)"

    def install_hint(self) -> str:
        return "pip install edge-tts"

    async def list_voices(self) -> list[dict]:
        return self.VOICES

    @staticmethod
    def _rate_str(speed: float | None) -> str:
        if not speed:
            return "+0%"
        return f"{round((speed - 1) * 100):+d}%"

    async def _render_mp3(self, payload: TTSRequest) -> bytes:
        """Real synthesis -> MP3 bytes, no artifact side effect."""
        try:
            import edge_tts
        except ImportError as exc:
            raise ProviderError(f"{self.name} synthesis failed: edge-tts not installed") from exc

        voice = payload.voice or self.DEFAULT_VOICE
        rate = self._rate_str(payload.speed)

        last_error: Exception | None = None
        chunks = bytearray()
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            chunks = bytearray()
            try:
                communicate = edge_tts.Communicate(payload.text, voice=voice, rate=rate)
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        chunks.extend(chunk["data"])
            except Exception as exc:  # noqa: BLE001 - retried below, surfaced as ProviderError if exhausted
                last_error = exc
            else:
                if chunks:
                    break
                last_error = None
            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(_RETRY_DELAY_SECONDS)
        else:
            if last_error is not None:
                raise ProviderError(
                    f"{self.name} synthesis failed after {_MAX_ATTEMPTS} attempts: {last_error}"
                ) from last_error
            raise ProviderError(f"{self.name} synthesis failed after {_MAX_ATTEMPTS} attempts: no audio received")

        return bytes(chunks)

    async def render_audio(self, payload: TTSRequest) -> tuple[bytes, str]:
        mp3_bytes = await self._render_mp3(payload)
        return mp3_bytes, "audio/mpeg"
