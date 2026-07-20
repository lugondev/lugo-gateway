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

# The conversation LLM's base_url/api_key/model live in the single Model
# Registry entry with kind="llm" that's marked `is_default` (and must also be
# `enabled` -- a disabled default fails closed to "no active LLM") -- there is
# no separate system-wide default and no in-memory override anymore; picking a
# different LLM (or pointing at a different endpoint) means re-pointing the
# default registry entry (see set_active_llm_config below).


async def _active_llm_entry() -> dict | None:
    from app.services.model_registry.store import model_registry_store

    entry = await model_registry_store.find_default(kind="llm")
    return entry if entry and entry["enabled"] else None


async def get_active_llm_model() -> str:
    entry = await _active_llm_entry()
    return entry["model_id"] if entry else ""


async def get_active_llm_base_url() -> str:
    entry = await _active_llm_entry()
    return entry["base_url"] if entry else ""


async def get_active_llm_api_key() -> str:
    entry = await _active_llm_entry()
    return entry["api_key"] if entry else ""


async def set_active_llm_config(base_url: str, api_key: str, model: str, engine: str = "custom") -> None:
    """Point the conversation responder at an OpenAI-compatible endpoint --
    creates the registry entry if none is the default yet, else updates the
    currently-default one in place (so re-pointing the same "slot" doesn't
    pile up rows)."""
    from app.services.model_registry.store import model_registry_store

    base_url = (base_url or "").strip()
    api_key = (api_key or "").strip()
    model = (model or "").strip()
    entry = await _active_llm_entry()
    if entry:
        # Leave `engine` untouched on an update -- it's a cosmetic label (only
        # `kind="llm" + is_default` matters for resolution), and always
        # defaulting the caller's `engine` param to "custom" here would stomp
        # a more specific tag (e.g. "ollama") set when the entry was first
        # created.
        await model_registry_store.set_fields(
            entry["id"], base_url=base_url, api_key=api_key, model_id=model
        )
    else:
        # New "slot": must be marked is_default too, or _active_llm_entry()
        # (which now resolves via find_default, not find_enabled) would never
        # find it again -- this legacy single-endpoint setter would silently
        # stop activating the LLM it just created.
        await model_registry_store.create(
            kind="llm", engine=engine, model_id=model, label="Conversation LLM",
            base_url=base_url, api_key=api_key, is_default=True,
        )


async def reset_active_llm_config() -> None:
    """Turn off the conversation LLM (disables the current is_default entry --
    conversation falls back to the built-in echo responder)."""
    from app.services.model_registry.store import model_registry_store

    entry = await _active_llm_entry()
    if entry:
        await model_registry_store.set_fields(entry["id"], enabled=False)


async def resolve_llm_override_from_registry(engine: str, model: str) -> tuple[str, str] | None:
    """Look up a Model Registry entry (kind="llm") for (engine, model). If it
    exists and carries its own api_key, its (base_url, api_key) take priority
    over a profile's inline llm.base_url/api_key -- this is what lets an admin
    set the key once, per model, in Model Registry instead of duplicating it
    into every profile. Returns None (no override) when engine/model are blank
    or no matching, keyed entry exists -- callers should fall back to their
    prior behavior."""
    if not engine or not model:
        return None
    from app.services.model_registry.store import model_registry_store

    entry = await model_registry_store.find(kind="llm", engine=engine, model_id=model)
    if entry and entry["api_key"]:
        return (entry["base_url"], entry["api_key"])
    return None


class Responder(ABC):
    name: str

    @abstractmethod
    async def reply(self, history: list[dict]) -> str:
        """Given chat history [{role, content}, ...] return the assistant reply."""
        raise NotImplementedError

    async def aclose(self) -> None:
        """Release any resources (e.g. a persistent HTTP client) held for this
        responder's lifetime. No-op by default; OpenAICompatResponder overrides
        this to close its reused httpx.AsyncClient."""

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
        # One client for the responder's whole lifetime (a session usually runs
        # many turns, each with 1-2+ LLM calls) instead of a fresh
        # httpx.AsyncClient per call -- httpx keeps the underlying TCP+TLS
        # connection alive between calls, so only the FIRST call to the LLM
        # host pays handshake latency instead of every single one. Must be
        # closed via aclose() when the responder is done (see call sites).
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def reply(self, history: list[dict]) -> str:
        messages = [{"role": "system", "content": self.system_prompt}, *history]
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            resp = await self._client.post(
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
                f"LLM offline ({self.model} @ {self.base_url}): {exc}"
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
            resp = await self._client.post(
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
            async with self._client.stream(
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
                f"LLM offline ({self.model} @ {self.base_url}): {exc}"
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


async def build_responder() -> Responder:
    base_url = await get_active_llm_base_url()
    if base_url:
        return OpenAICompatResponder(
            base_url=base_url,
            api_key=await get_active_llm_api_key(),
            model=await get_active_llm_model(),
            system_prompt=resolve_system_prompt(None),
            timeout=system_config_store.get().conversation.llm_timeout_seconds,
        )
    return EchoResponder()


async def build_responder_ex(
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    system_prompt: str | None = None,
    voice_optimized: bool = False,
) -> Responder:
    """Build a responder with optional overrides; falls back to the active
    Model Registry LLM entry.

    Passing None for any arg uses the current active config value.
    """
    effective_url = base_url if base_url is not None else await get_active_llm_base_url()
    if effective_url:
        return OpenAICompatResponder(
            base_url=effective_url,
            api_key=api_key if api_key is not None else await get_active_llm_api_key(),
            model=model if model is not None else await get_active_llm_model(),
            system_prompt=resolve_system_prompt(system_prompt, voice_optimized),
            timeout=system_config_store.get().conversation.llm_timeout_seconds,
        )
    return EchoResponder()
