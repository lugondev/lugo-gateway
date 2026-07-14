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
from typing import TYPE_CHECKING

import httpx

from app.core.errors import LLMUnavailableError
from app.services.system_config import system_config_store
from app.services.tts.segmenter import SentenceAggregator, segment_text

if TYPE_CHECKING:
    from app.services.conversation.tools.base import ToolContext, ToolRegistry

logger = logging.getLogger(__name__)

# Runtime conversation LLM config; each falls back to settings when None.
# Reset on restart (not persisted). base_url/api_key let the UI point the
# conversation at any OpenAI-compatible endpoint (local Ollama OR an online
# provider like OpenAI/Groq/together); empty base_url -> built-in echo.
_active_model: str | None = None
_active_base_url: str | None = None
_active_api_key: str | None = None


def get_active_llm_model() -> str:
    return _active_model or system_config_store.get().conversation_llm.conversation_llm_model


def set_active_llm_model(model: str) -> None:
    global _active_model
    _active_model = model


def get_active_llm_base_url() -> str:
    if _active_base_url is not None:
        return _active_base_url
    return system_config_store.get().conversation_llm.conversation_llm_base_url


def get_active_llm_api_key() -> str:
    if _active_api_key is not None:
        return _active_api_key
    return system_config_store.get().conversation_llm.conversation_llm_api_key


def set_active_llm_config(base_url: str, api_key: str, model: str) -> None:
    """Point the conversation responder at an OpenAI-compatible endpoint."""
    global _active_base_url, _active_api_key, _active_model
    _active_base_url = (base_url or "").strip()
    _active_api_key = (api_key or "").strip()
    _active_model = (model or "").strip() or None


def reset_active_llm_config() -> None:
    """Revert to the .env-configured conversation LLM."""
    global _active_base_url, _active_api_key, _active_model
    _active_base_url = _active_api_key = _active_model = None


class Responder(ABC):
    name: str

    @abstractmethod
    async def reply(self, history: list[dict]) -> str:
        """Given chat history [{role, content}, ...] return the assistant reply."""
        raise NotImplementedError

    async def reply_stream(
        self,
        history: list[dict],
        registry: "ToolRegistry | None" = None,
        ctx: "ToolContext | None" = None,
        max_iters: int = 3,
    ) -> AsyncIterator[str]:
        """Yield the reply sentence-by-sentence for low-latency TTS.

        Default: produce the full reply, then segment it. LLM backends override
        this to stream tokens and emit sentences as they complete.
        ``registry``/``ctx``/``max_iters`` are accepted for interface compatibility
        but ignored by non-LLM responders.
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

    async def reply_stream(
        self,
        history: list[dict],
        registry: "ToolRegistry | None" = None,
        ctx: "ToolContext | None" = None,
        max_iters: int = 3,
    ) -> AsyncIterator[str]:
        """Stream the assistant reply sentence-by-sentence.

        When *registry* is provided the method runs a 2-phase tool-calling loop
        before streaming:
          1. Non-streaming detect POST — checks ``choices[0].message.tool_calls``.
          2. If tool_calls present: run each tool, append result messages, repeat
             up to *max_iters*.
          3. Stream final content from the (possibly augmented) history.
        """
        if registry is not None and len(registry) > 0:
            async for chunk in self._tool_then_stream(history, registry, ctx, max_iters):
                yield chunk
        else:
            async for chunk in self._stream_history(history):
                yield chunk

    async def _tool_then_stream(
        self,
        history: list[dict],
        registry: "ToolRegistry",
        ctx: "ToolContext | None",
        max_iters: int,
    ) -> AsyncIterator[str]:
        from app.services.conversation.tools.base import ToolContext as _TC

        working = list(history)
        ctx = ctx or _TC()
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

        for _ in range(max_iters):
            messages = [{"role": "system", "content": self.system_prompt}, *working]
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json={"model": self.model, "messages": messages, "tools": registry.openai_schema()},
                )
                resp.raise_for_status()
                data = resp.json()

            assistant_msg = data["choices"][0]["message"]
            tool_calls = assistant_msg.get("tool_calls")
            logger.info("DEBUG_HANG _tool_then_stream: iter=%d tool_calls=%s", _, bool(tool_calls))
            if not tool_calls:
                break

            working.append(assistant_msg)
            for tc in tool_calls:
                tool_name = tc["function"]["name"]
                try:
                    tool_args = json.loads(tc["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    tool_args = {}
                logger.info("DEBUG_HANG _tool_then_stream: running tool %s", tool_name)
                result = await registry.run(tool_name, tool_args, ctx)
                logger.info("DEBUG_HANG _tool_then_stream: tool %s done", tool_name)
                working.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

        logger.info("DEBUG_HANG _tool_then_stream: handing off to _stream_history, %d messages", len(working))
        async for chunk in self._stream_history(working):
            yield chunk

    async def _stream_history(self, history: list[dict]) -> AsyncIterator[str]:
        messages = [{"role": "system", "content": self.system_prompt}, *history]
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        agg = SentenceAggregator()
        logger.info("DEBUG_HANG _stream_history: opening stream request")
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json={"model": self.model, "messages": messages, "stream": True},
                ) as resp:
                    logger.info("DEBUG_HANG _stream_history: got response headers, status=%s", resp.status_code)
                    resp.raise_for_status()
                    line_count = 0
                    async for line in resp.aiter_lines():
                        line_count += 1
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            logger.info("DEBUG_HANG _stream_history: saw [DONE] after %d lines", line_count)
                            break
                        delta = json.loads(data)["choices"][0].get("delta", {}).get("content", "")
                        if delta:
                            for sentence in agg.push(delta):
                                logger.info("DEBUG_HANG _stream_history: yielding sentence %r", sentence)
                                yield sentence
                    else:
                        logger.info("DEBUG_HANG _stream_history: aiter_lines exhausted without [DONE], %d lines", line_count)
            logger.info("DEBUG_HANG _stream_history: stream closed, flushing aggregator")
            for sentence in agg.flush():
                logger.info("DEBUG_HANG _stream_history: yielding flushed sentence %r", sentence)
                yield sentence
            logger.info("DEBUG_HANG _stream_history: done")
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM stream failed: %s", exc)
            raise LLMUnavailableError(
                f"LLM offline ({self.model}) — start the Ollama service (System tab) "
                "or check CONVERSATION_LLM_BASE_URL."
            ) from exc


VOICE_OPTIMIZATION_DIRECTIVE = (
    "Câu trả lời của bạn sẽ được đọc thành giọng nói (text-to-speech), nên hãy "
    "viết ở dạng văn nói tự nhiên, thuần văn bản, dễ đọc thành tiếng:\n"
    "- Không dùng markdown hay ký tự định dạng (* _ # ` ~ | > -), không in đậm, "
    "in nghiêng, tiêu đề hay khối mã.\n"
    "- Không dùng emoji, biểu tượng hay ký hiệu đặc biệt.\n"
    "- Không dùng gạch đầu dòng hay danh sách đánh số; nếu cần liệt kê thì viết "
    "thành câu, dùng từ nối như 'thứ nhất', 'thứ hai', hoặc ngăn cách bằng dấu phẩy.\n"
    "- Viết số, đơn vị, ngày giờ và ký hiệu bằng chữ dễ đọc (ví dụ '50 phần trăm' "
    "thay vì '50%', 'đô la' thay vì '$').\n"
    "- Không chèn URL hay đường link dài; nếu cần thì mô tả bằng lời.\n"
    "- Giữ câu ngắn gọn, tự nhiên như đang trò chuyện."
)


def resolve_system_prompt(system_prompt: str | None, voice_optimized: bool = False) -> str:
    """Resolve the persona prompt (explicit override or .env default) and always
    prepend the user-configured base context (platform intro + guardrail rules),
    if any, so it applies regardless of profile.

    When voice_optimized is set, append the speakable-text directive to the very
    end so it survives memory injection (which prepends) and stays most salient."""
    persona = (
        system_prompt
        if system_prompt is not None
        else system_config_store.get().conversation.conversation_system_prompt
    )
    base_context = system_config_store.get().base_context
    prompt = f"{base_context}\n\n{persona}" if base_context else persona
    if voice_optimized:
        prompt = f"{prompt}\n\n{VOICE_OPTIMIZATION_DIRECTIVE}"
    return prompt


def build_responder() -> Responder:
    base_url = get_active_llm_base_url()
    if base_url:
        return OpenAICompatResponder(
            base_url=base_url,
            api_key=get_active_llm_api_key(),
            model=get_active_llm_model(),
            system_prompt=resolve_system_prompt(None),
            timeout=system_config_store.get().conversation_llm.conversation_llm_timeout_seconds,
        )
    return EchoResponder()


def build_responder_ex(
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    system_prompt: str | None = None,
    voice_optimized: bool = False,
) -> Responder:
    """Build a responder with optional overrides; falls back to .env defaults.

    Passing None for any arg uses the current global active config value.
    """
    effective_url = base_url if base_url is not None else get_active_llm_base_url()
    if effective_url:
        return OpenAICompatResponder(
            base_url=effective_url,
            api_key=api_key if api_key is not None else get_active_llm_api_key(),
            model=model if model is not None else get_active_llm_model(),
            system_prompt=resolve_system_prompt(system_prompt, voice_optimized),
            timeout=system_config_store.get().conversation_llm.conversation_llm_timeout_seconds,
        )
    return EchoResponder()
