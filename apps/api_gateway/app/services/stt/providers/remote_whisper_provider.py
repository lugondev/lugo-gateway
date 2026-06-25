import httpx

from app.schemas.stt import STTResult
from app.services.stt.base import STTProvider


class RemoteWhisperProvider(STTProvider):
    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.name = name
        self.base_url = base_url.strip()
        self.api_key = api_key.strip()
        self.model = model
        self.timeout_seconds = timeout_seconds

    async def transcribe_bytes(self, audio_bytes: bytes, language: str | None = None) -> STTResult:
        if not self.base_url:
            raise RuntimeError(
                f"{self.name} is not configured. Set base URL in environment settings."
            )

        endpoint = f"{self.base_url.rstrip('/')}/audio/transcriptions"
        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        data = {
            "model": self.model,
            "response_format": "json",
        }
        if language:
            data["language"] = language

        files = {
            "file": ("audio.wav", audio_bytes, "audio/wav"),
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(endpoint, headers=headers, data=data, files=files)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"{self.name} returned HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"{self.name} request failed: {exc}") from exc

        text = str(payload.get("text", "")).strip()
        return STTResult(engine=self.name, text=text, is_final=True, confidence=None)
