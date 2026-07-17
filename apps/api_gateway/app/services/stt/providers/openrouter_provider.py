import base64

import httpx

from app.schemas.stt import STTResult
from app.services.http_errors import translate_httpx_error
from app.services.model_registry.store import model_registry_store
from app.services.stt.base import STTProvider

_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterSttProvider(STTProvider):
    """STT via OpenRouter's dedicated /audio/transcriptions endpoint.

    Audio is sent as a base64 `input_audio` field of the (non-chat) JSON body;
    the transcript comes back directly as `{"text": ...}`. This endpoint is
    purpose-built for transcription models (Whisper, GPT-4o Transcribe, Chirp 3,
    Qwen3-ASR, ...) -- unlike /chat/completions, which some transcription-only
    models (e.g. openai/whisper-large-v3-turbo) reject outright.

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

        body = {
            "model": effective_model,
            "input_audio": {
                "data": base64.b64encode(audio_bytes).decode("ascii"),
                "format": "wav",
            },
        }
        if language:
            body["language"] = language
        headers = {"Authorization": f"Bearer {api_key}"}

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    f"{_BASE_URL}/audio/transcriptions", headers=headers, json=body
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            raise translate_httpx_error(self.name, exc) from exc

        text = str(payload["text"]).strip()
        return STTResult(engine=self.name, text=text, is_final=True, confidence=None)
