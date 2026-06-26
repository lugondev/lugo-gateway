"""Reply generation for voice conversation.

Two responders:
- EchoResponder: built-in, no external service — acknowledges the user's turn.
- OpenAICompatResponder: POST to any OpenAI-compatible /chat/completions
  (Ollama, LM Studio, vLLM, OpenAI) for real LLM replies.

The factory picks the LLM responder when a base URL is configured, else echo.
"""

import logging
from abc import ABC, abstractmethod

import httpx

from app.core.settings import settings

logger = logging.getLogger(__name__)


class Responder(ABC):
    name: str

    @abstractmethod
    async def reply(self, history: list[dict]) -> str:
        """Given chat history [{role, content}, ...] return the assistant reply."""
        raise NotImplementedError


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
        except Exception as exc:  # noqa: BLE001 - never break the turn loop
            logger.warning("LLM responder failed (%s); falling back to echo", exc)
            return await EchoResponder().reply(history)


def build_responder() -> Responder:
    if settings.conversation_llm_base_url:
        return OpenAICompatResponder(
            base_url=settings.conversation_llm_base_url,
            api_key=settings.conversation_llm_api_key,
            model=settings.conversation_llm_model,
            system_prompt=settings.conversation_system_prompt,
            timeout=settings.conversation_llm_timeout_seconds,
        )
    return EchoResponder()
