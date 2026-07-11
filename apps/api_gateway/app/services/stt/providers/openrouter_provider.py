import base64

import httpx

from app.schemas.stt import STTResult
from app.services.stt.base import STTProvider
from app.services.system_config import system_config_store

_BASE_URL = "https://openrouter.ai/api/v1"

_PROMPT = "Transcribe this audio recording verbatim. Respond with only the transcript text, no extra commentary."


class OpenRouterSttProvider(STTProvider):
    """STT via OpenRouter chat/completions with an `input_audio` content part.

    OpenRouter has no dedicated /audio/transcriptions endpoint (unlike OpenAI-
    compatible whisper_service): audio is sent as a base64 `input_audio` part
    of a chat message, and the transcript is read back from the assistant's
    reply text.
    """

    def __init__(self, name: str, model: str, timeout_seconds: float = 60.0) -> None:
        self.name = name
        self.model = model
        self.timeout_seconds = timeout_seconds

    async def transcribe_bytes(self, audio_bytes: bytes, language: str | None = None) -> STTResult:
        api_key = system_config_store.get().openrouter_api_key
        if not api_key:
            raise RuntimeError(
                f"{self.name} is not configured. Set the OpenRouter API key in system config."
            )

        prompt = _PROMPT
        if language:
            prompt += f" The spoken language is {language}."

        body = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": base64.b64encode(audio_bytes).decode("ascii"),
                                "format": "wav",
                            },
                        },
                    ],
                }
            ],
        }
        headers = {"Authorization": f"Bearer {api_key}"}

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    f"{_BASE_URL}/chat/completions", headers=headers, json=body
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"{self.name} returned HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"{self.name} request failed: {exc}") from exc

        text = str(payload["choices"][0]["message"]["content"]).strip()
        return STTResult(engine=self.name, text=text, is_final=True, confidence=None)
