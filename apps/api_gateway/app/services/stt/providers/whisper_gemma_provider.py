"""Enhanced STT: faster-whisper transcript refined by the conversation LLM (Gemma).

Runs the local Whisper model, then asks the configured OpenAI-compatible LLM
(Ollama / Gemma by default) to correct ASR errors, punctuation and casing — a
big quality win for Vietnamese. Degrades gracefully to the raw Whisper text when
no LLM is configured or the call fails.
"""

import logging

import httpx

from app.core.settings import settings
from app.schemas.stt import STTResult
from app.services.conversation.responder import get_active_llm_model
from app.services.stt.base import STTProvider
from app.services.stt.providers.whisper_provider import WhisperProvider, get_active_whisper_model

logger = logging.getLogger(__name__)


class WhisperGemmaProvider(STTProvider):
    name = "whisper_gemma"

    def __init__(self) -> None:
        self._whisper = WhisperProvider()

    def detail(self) -> str:
        return f"{get_active_whisper_model()} → {get_active_llm_model()} refine"

    async def _refine(self, text: str, language: str | None) -> str:
        base = settings.conversation_llm_base_url
        if not base:
            return text  # no LLM configured -> raw transcript
        headers = {"Authorization": f"Bearer {settings.conversation_llm_api_key}"} if settings.conversation_llm_api_key else {}
        lang = language or "the same language as the transcript"
        messages = [
            {"role": "system", "content": settings.stt_enhance_prompt},
            {"role": "user", "content": f"Language: {lang}\nTranscript: {text}"},
        ]
        try:
            async with httpx.AsyncClient(timeout=settings.stt_enhance_timeout_seconds) as client:
                resp = await client.post(
                    f"{base.rstrip('/')}/chat/completions",
                    headers=headers,
                    json={"model": get_active_llm_model(), "messages": messages, "temperature": 0},
                )
                resp.raise_for_status()
                data = resp.json()
            refined = str(data["choices"][0]["message"]["content"]).strip()
            return refined or text
        except Exception as exc:  # noqa: BLE001 - never fail STT on refinement
            logger.warning("whisper_gemma refine failed (%s); returning raw transcript", exc)
            return text

    async def transcribe_bytes(
        self, audio_bytes: bytes, language: str | None = None, model: str | None = None
    ) -> STTResult:
        base = await self._whisper.transcribe_bytes(audio_bytes, language, model)
        text = (base.text or "").strip()
        if not text:
            return STTResult(engine=self.name, text="", is_final=True)
        refined = await self._refine(text, language)
        return STTResult(engine=self.name, text=refined, is_final=True)
