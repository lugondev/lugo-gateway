"""Build the memory context block injected into the system prompt each turn."""

from __future__ import annotations

import logging

from app.services.memory.embedder import cosine, embed_texts
from app.services.memory.store import memory_store
from app.services.profiles.models import Profile

logger = logging.getLogger(__name__)

MAX_ITEMS = 50
MAX_CHARS = 2000


def inject_memories(system_prompt: str, block: str) -> str:
    if not block:
        return system_prompt
    if not system_prompt:
        return block
    return f"{block}\n\n{system_prompt}"


class MemoryRetriever:
    async def get_context(self, profile: Profile | None, query: str = "") -> str:
        if profile is None or not profile.memory.enabled:
            return ""
        items = await memory_store.list(profile.name)
        if not items:
            return ""
        if profile.memory.mode == "semantic" and query:
            items = await self._semantic_filter(items, query, profile)
        contents: list[str] = []
        total = 0
        for item in items[:MAX_ITEMS]:
            content = item["content"]
            if total + len(content) > MAX_CHARS:
                break
            contents.append(content)
            total += len(content)
        if not contents:
            return ""
        return "## User Memories\n" + "\n".join(f"- {c}" for c in contents)

    async def _semantic_filter(
        self, items: list[dict], query: str, profile: Profile
    ) -> list[dict]:
        """Top-k by cosine similarity; falls back to the full list on any gap."""
        with_vec = [i for i in items if i.get("embedding")]
        if not with_vec or not profile.memory.embed_model or not profile.llm.base_url:
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
