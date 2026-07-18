"""Build the memory context block injected into the system prompt each turn."""

from __future__ import annotations

import logging

from app.services.memory.embedder import cosine, embed_texts
from app.services.memory.store import memory_store, profile_doc_store
from app.services.profiles.models import Profile

logger = logging.getLogger(__name__)

MAX_ITEMS = 50
MAX_CHARS = 2000
MAX_DOC_CHARS = 1500  # leaves >=500 of MAX_CHARS for recent notes


def _truncate_at_boundary(text: str, limit: int) -> str:
    """Truncate `text` to at most `limit` chars without cutting mid-line."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    head = cut.rsplit("\n", 1)[0]
    if head:
        cut = head
    return cut.rstrip()


def inject_memories(system_prompt: str, block: str) -> str:
    if not block:
        return system_prompt
    if not system_prompt:
        return block
    return f"{block}\n\n{system_prompt}"


class MemoryRetriever:
    async def get_context(
        self, profile: Profile | None, query: str = "", user_id: str | None = None
    ) -> str:
        if profile is None or not profile.memory.enabled:
            return ""
        doc = await profile_doc_store.get(profile.name, user_id=user_id)
        doc_block = doc["content"].strip() if doc and doc["content"] else ""
        doc_block = _truncate_at_boundary(doc_block, MAX_DOC_CHARS)
        items = await memory_store.list(profile.name, user_id=user_id)
        if profile.memory.mode == "semantic" and query and items:
            items = await self._semantic_filter(items, query, profile)
        buffer_lines: list[str] = []
        total = len(doc_block)
        header = "## Recent notes\n"
        for item in items[:MAX_ITEMS]:
            content = item["content"]
            line = f"- {content}"
            extra = len(line) + 1  # joining newline between buffer lines
            if not buffer_lines:
                extra += len(header)
                if doc_block:
                    extra += 2  # "\n\n" separator joining the doc and notes parts
            if total + extra > MAX_CHARS:
                break
            buffer_lines.append(line)
            total += extra
        parts: list[str] = []
        if doc_block:
            parts.append(doc_block)
        if buffer_lines:
            parts.append(header + "\n".join(buffer_lines))
        return "\n\n".join(parts)

    async def _semantic_filter(
        self, items: list[dict], query: str, profile: Profile
    ) -> list[dict]:
        """Top-k by cosine similarity; falls back to the full list on any gap."""
        with_vec = [i for i in items if i.get("embedding")]
        if not with_vec or not profile.memory.embed_model or not profile.llm.base_url:
            logger.warning(
                "semantic memory mode for profile %s falling back to all: %s",
                profile.name,
                "no stored embeddings" if not with_vec else "embed_model/base_url not configured",
            )
            return items
        try:
            qvec = (
                await embed_texts(
                    [query], profile.llm.base_url, profile.llm.api_key,
                    profile.memory.embed_model,
                )
            )[0]
        except Exception as exc:  # noqa: BLE001 - fall back to all memories
            logger.warning("semantic memory embed failed, using all: %s", exc)
            return items
        scored = sorted(
            with_vec, key=lambda i: cosine(qvec, i["embedding"]), reverse=True
        )
        return scored[: profile.memory.top_k]


memory_retriever = MemoryRetriever()
