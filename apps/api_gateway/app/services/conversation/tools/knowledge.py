"""The `search_knowledge` tool.

The description is the feature. With always-inject the model sees the content
whether it wants it or not; with a tool it must choose to call, and it chooses
from the description alone. That text is the operator's, not ours.
"""

from __future__ import annotations

import logging

from app.services.conversation.tools.base import Tool, ToolContext, ToolSource

logger = logging.getLogger(__name__)

MAX_CHARS = 2000
UNAVAILABLE = "The knowledge base could not be reached just now."
NO_HITS = "No matching documents in the knowledge base."

_PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "What to look up, in the user's own words.",
        }
    },
    "required": ["query"],
}


def _render(hits: list[dict], limit: int = MAX_CHARS) -> str:
    """Heading path then text, so the model can attribute what it answers."""
    parts: list[str] = []
    total = 0
    for hit in hits:
        title = (hit.get("title") or hit.get("filename") or "").strip()
        heading = (hit.get("heading") or "").strip()
        path = " > ".join(p for p in (title, heading) if p)
        text = (hit.get("text") or "").strip()
        if not text:
            continue
        block = f"### {path}\n{text}" if path else text
        extra = len(block) + (2 if parts else 0)
        if total + extra > limit:
            break
        parts.append(block)
        total += extra
    return "\n\n".join(parts)


class KnowledgeToolSource(ToolSource):
    def __init__(self, profile, client, *, user_id: str | None = None) -> None:
        self._profile = profile
        self._client = client
        self._user_id = user_id or ""

    def _description(self) -> str:
        written = (self._profile.knowledge.description or "").strip()
        if written:
            return written
        # Never empty: a tool with no description is one the model cannot judge.
        return (
            f"Search the '{self._profile.knowledge.collection}' knowledge base for "
            "reference material. Prefer it over guessing when the user asks about "
            "documented facts."
        )

    def list_tools(self) -> list[Tool]:
        return [
            Tool(
                name="search_knowledge",
                description=self._description(),
                parameters=_PARAMETERS,
                run=self._run,
            )
        ]

    async def _run(self, args: dict, ctx: ToolContext) -> str:
        cfg = self._profile.knowledge
        query = (args or {}).get("query") or ""
        if not query.strip():
            return NO_HITS
        try:
            # limit and min_score come from the profile, never from `args`: a
            # model that picks the collection turns a prompt injection into a
            # cross-persona read, and one that picks top_k asks for fifty.
            hits, tokens = await self._client.search_with_usage(
                cfg.collection, query, limit=cfg.top_k, min_score=cfg.min_score
            )
        except Exception as exc:  # noqa: BLE001 - fail open; a raise kills the turn
            # Caught here rather than in ToolRegistry.run, which formats the
            # exception into its reply -- and an httpx error carries the base URL.
            logger.warning("knowledge search failed: %s", exc)
            return UNAVAILABLE
        await self._record(tokens)
        return _render(hits) or NO_HITS

    async def _record(self, tokens: int) -> None:
        if tokens <= 0:
            return
        from app.services.usage.recorder import record_usage

        await record_usage(
            user_id=self._user_id,
            profile_id=self._profile.name,
            kind="embed",
            engine=self._profile.llm.engine or "",
            model_id=self._profile.knowledge.embed_model,
            unit="tokens",
            native_amount=tokens,
            prompt_tokens=tokens,
        )
