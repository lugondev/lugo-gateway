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
# Hits existed but none of them fit. Distinct from NO_HITS on purpose: telling
# the model the knowledge base holds nothing, when it holds something too long
# to quote, is a false negative it cannot recover from.
TOO_LONG = "Matching documents were found but are too long to quote here."

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


def _cut_at_line(text: str, room: int) -> str:
    """As much of `text` as fits in `room`, ending on a line boundary.

    Spec, *Result shape*: truncated at a line boundary, never mid-line. A chunk
    with no line break inside the budget therefore yields nothing -- the caller
    skips it and tries the next hit rather than emitting half a sentence.
    """
    if len(text) <= room:
        return text
    head = text[:room]
    cut = head.rfind("\n")
    if cut <= 0:
        return ""
    return head[:cut].rstrip()


def _render(hits: list[dict], limit: int = MAX_CHARS) -> str:
    """Heading path then text, so the model can attribute what it answers.

    Over-budget hits are truncated at a line boundary and, failing that,
    skipped -- never `break`. A `break` on the first oversized hit left `parts`
    empty, which _run read as "no matching documents" while holding usable
    ones, and discarded every later hit as well.
    """
    parts: list[str] = []
    total = 0
    for hit in hits:
        title = (hit.get("title") or hit.get("filename") or "").strip()
        heading = (hit.get("heading") or "").strip()
        path = " > ".join(p for p in (title, heading) if p)
        text = (hit.get("text") or "").strip()
        if not text:
            continue
        prefix = f"### {path}\n" if path else ""
        sep = 2 if parts else 0
        room = limit - total - sep - len(prefix)
        if room <= 0:
            # No room for this hit's heading alone; a shorter later hit may
            # still fit, so keep going instead of stopping here.
            continue
        fitted = _cut_at_line(text, room)
        if not fitted:
            continue
        block = prefix + fitted
        parts.append(block)
        total += len(block) + sep
    return "\n\n".join(parts)


def _query_of(args) -> str:
    """The model's `query`, or "" if it sent something that is not one.

    A model emitting {"query": 123} or a non-object arguments blob used to
    escape into ToolRegistry.run's generic handler as "Error running
    search_knowledge: 'int' object has no attribute 'strip'". The spec mandates
    never-an-exception for this tool specifically. Numbers are coerced (a model
    asking for a part number means it); structures are refused.
    """
    if not isinstance(args, dict):
        return ""
    raw = args.get("query")
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, bool) or raw is None:
        return ""
    if isinstance(raw, (int, float)):
        return str(raw).strip()
    return ""


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
        query = _query_of(args)
        if not query:
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
        rendered = _render(hits)
        if rendered:
            return rendered
        # Empty with hits in hand means every one of them was unquotable, not
        # that the collection had nothing to say.
        return TOO_LONG if hits else NO_HITS

    async def _record(self, tokens: int) -> None:
        if tokens <= 0:
            return
        from app.services.usage.recorder import record_usage

        await record_usage(
            user_id=self._user_id,
            profile_id=self._profile.name,
            kind="embed",
            # Deliberately blank, NOT profile.llm.engine. kbase embeds with its
            # own provider under its own KB_EMBED_MODEL; the gateway's chat LLM
            # has no relationship to it. resolve_usage_model returns (engine,
            # model_id) unchanged when BOTH are non-blank, so a wrong engine
            # made the registry lookup miss -> provider_id "" -> cost $0
            # forever, the exact failure the spec's Metering section warns
            # about. Blank lets attribution derive the engine from the declared
            # embed model, which is the only end of the key the gateway
            # actually knows. (The memory precedent this copied is not
            # analogous: memory embeds through profile.llm.base_url, so there
            # the engine really did spend the money.)
            engine="",
            model_id=self._profile.knowledge.embed_model,
            unit="tokens",
            native_amount=tokens,
            prompt_tokens=tokens,
        )
