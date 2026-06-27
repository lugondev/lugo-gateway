"""Reply generation for voice conversation.

Two responders:
- EchoResponder: built-in, no external service — acknowledges the user's turn.
- OpenAICompatResponder: POST to any OpenAI-compatible /chat/completions
  (Ollama, LM Studio, vLLM, OpenAI) for real LLM replies.

The factory picks the LLM responder when a base URL is configured, else echo.
"""

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

import httpx

from app.core.errors import LLMUnavailableError
from app.core.settings import settings
from app.services.tts.segmenter import SentenceAggregator, segment_text

logger = logging.getLogger(__name__)

# Runtime-selected conversation LLM model; falls back to settings. Reset on restart.
_active_model: str | None = None


def get_active_llm_model() -> str:
    return _active_model or settings.conversation_llm_model


def set_active_llm_model(model: str) -> None:
    global _active_model
    _active_model = model


class Responder(ABC):
    name: str

    @abstractmethod
    async def reply(self, history: list[dict]) -> str:
        """Given chat history [{role, content}, ...] return the assistant reply."""
        raise NotImplementedError

    async def reply_stream(self, history: list[dict]) -> AsyncIterator[str]:
        """Yield the reply sentence-by-sentence for low-latency TTS.

        Default: produce the full reply, then segment it. LLM backends override
        this to stream tokens and emit sentences as they complete.
        """
        reply = await self.reply(history)
        for sentence in segment_text(reply) or ([reply] if reply else []):
            yield sentence


class EchoResponder(Responder):
    name = "echo"

    async def reply(self, history: list[dict]) -> str:
        last = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
        last = (last or "").strip()
        if not last:
            return "Mình chưa nghe rõ, bạn nói lại giúp nhé."
        return (
            f"Bạn vừa nói: {last}. "
            "Mình đã ghi nhận và đang phản hồi lại bằng giọng nói. "
            "Bạn cứ tiếp tục nói, mình sẽ tự nhận biết khi bạn ngừng để trả lời."
        )


class OpenAICompatResponder(Responder):
    name = "llm"

    def __init__(self, base_url: str, api_key: str, model: str, system_prompt: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.system_prompt = system_prompt
        self.timeout = timeout

    async def reply(self, history: list[dict]) -> str:
        messages = [{"role": "system", "content": self.system_prompt}, *history]
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json={"model": self.model, "messages": messages},
                )
                resp.raise_for_status()
                data = resp.json()
            return str(data["choices"][0]["message"]["content"]).strip()
        except Exception as exc:  # noqa: BLE001 - surface a clear offline error
            logger.warning("LLM responder failed: %s", exc)
            raise LLMUnavailableError(
                f"LLM offline ({self.model}) — start the Ollama service (System tab) "
                "or check CONVERSATION_LLM_BASE_URL."
            ) from exc

    async def reply_stream(self, history: list[dict]) -> AsyncIterator[str]:
        messages = [{"role": "system", "content": self.system_prompt}, *history]
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        agg = SentenceAggregator()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json={"model": self.model, "messages": messages, "stream": True},
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        delta = json.loads(data)["choices"][0].get("delta", {}).get("content", "")
                        if delta:
                            for sentence in agg.push(delta):
                                yield sentence
            for sentence in agg.flush():
                yield sentence
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM stream failed: %s", exc)
            raise LLMUnavailableError(
                f"LLM offline ({self.model}) — start the Ollama service (System tab) "
                "or check CONVERSATION_LLM_BASE_URL."
            ) from exc


def build_responder() -> Responder:
    if settings.conversation_llm_base_url:
        return OpenAICompatResponder(
            base_url=settings.conversation_llm_base_url,
            api_key=settings.conversation_llm_api_key,
            model=get_active_llm_model(),
            system_prompt=settings.conversation_system_prompt,
            timeout=settings.conversation_llm_timeout_seconds,
        )
    return EchoResponder()
