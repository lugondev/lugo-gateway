import base64

import httpx

from app.schemas.stt import STTResult
from app.services.model_registry.store import model_registry_store
from app.services.stt.base import STTProvider

_BASE_URL = "https://openrouter.ai/api/v1"

_PROMPT = "Transcribe this audio recording verbatim. Respond with only the transcript text, no extra commentary."


class OpenRouterSttProvider(STTProvider):
    """STT via OpenRouter chat/completions with an `input_audio` content part.

    OpenRouter has no dedicated /audio/transcriptions endpoint (unlike OpenAI-
    compatible whisper_service): audio is sent as a base64 `input_audio` part
    of a chat message, and the transcript is read back from the assistant's
    reply text.

    No system-wide OpenRouter key: each Model Registry entry for this engine
    (kind="stt", engine=self.name, model_id=<model>) carries its own api_key,
    looked up at call time -- so different models can use different keys.
    `api_key` here is an explicit override, used only for the registry's
    test-before-add call (the entry doesn't exist yet at that point, so there
    is nothing to look up).
    """

    def __init__(
        self, name: str, model: str, timeout_seconds: float = 60.0, api_key: str | None = None
    ) -> None:
        self.name = name
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._api_key_override = api_key

    async def _resolve_api_key(self, model: str) -> str:
        if self._api_key_override is not None:
            return self._api_key_override
        entry = await model_registry_store.find(kind="stt", engine=self.name, model_id=model)
        return entry["api_key"] if entry else ""

    async def transcribe_bytes(
        self, audio_bytes: bytes, language: str | None = None, model: str | None = None
    ) -> STTResult:
        effective_model = model or self.model
        api_key = await self._resolve_api_key(effective_model)
        if not api_key:
            raise RuntimeError(
                f"{self.name} is not configured. Set this model's API key when adding it "
                "in Model Registry."
            )

        prompt = _PROMPT
        if language:
            prompt += f" The spoken language is {language}."

        body = {
            "model": effective_model,
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
