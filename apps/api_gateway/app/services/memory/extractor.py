"""Post-session memory extraction (mem0-inspired).

One LLM call over the session transcript -> JSON array of durable user facts
-> deduped upsert into MemoryStore. Failures are logged and swallowed: memory
extraction must never break a session teardown.
"""

from __future__ import annotations

import json
import logging

import httpx

from app.core.settings import settings
from app.services.history.store import session_store
from app.services.memory.store import memory_store
from app.services.profiles.models import Profile

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = (
    "You extract durable facts about the user from a conversation transcript. "
    "Return ONLY a JSON array of short fact strings, in the user's language, "
    'e.g. ["User prefers Vietnamese", "User is building an ESP32 assistant"]. '
    "Only include stable facts worth remembering across conversations "
    "(preferences, identity, projects, constraints, relationships). "
    "Do not include small talk or one-off requests. Return [] if none."
)

_decoder = json.JSONDecoder()


def _parse_facts(raw: str) -> list[str]:
    """Extract a JSON array of strings from an LLM reply (tolerant of prose/fences)."""
    text = raw or ""
    pos = text.find("[")
    while pos != -1:
        try:
            data, _ = _decoder.raw_decode(text, pos)
        except json.JSONDecodeError:
            pos = text.find("[", pos + 1)
            continue
        if not isinstance(data, list):
            pos = text.find("[", pos + 1)
            continue
        facts = [item.strip() for item in data if isinstance(item, str) and item.strip()]
        return facts if len(facts) == len(data) else []
    return []


class MemoryExtractor:
    async def extract(
        self, messages: list[dict], base_url: str, api_key: str, model: str
    ) -> list[str]:
        transcript = "\n".join(
            f"{m['role']}: {m['content']}"
            for m in messages
            if m.get("role") in ("user", "assistant") and m.get("content")
        )
        if not transcript:
            return []
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        try:
            async with httpx.AsyncClient(
                timeout=settings.conversation_llm_timeout_seconds
            ) as client:
                resp = await client.post(
                    f"{base_url.rstrip('/')}/chat/completions",
                    headers=headers,
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": EXTRACTION_PROMPT},
                            {"role": "user", "content": transcript},
                        ],
                    },
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001 - extraction is best-effort
            logger.warning("memory extraction LLM call failed: %s", exc)
            return []
        return _parse_facts(str(content))

    async def extract_and_upsert(self, session_id: str, profile: Profile) -> int:
        """Extract facts from a finished session into the profile's memory."""
        try:
            if not profile.memory.enabled or not profile.llm.base_url:
                return 0
            messages = await session_store.get_messages(session_id)
            if len(messages) < 2:
                return 0
            model = profile.memory.extractor_model or profile.llm.model
            facts = await self.extract(
                messages, profile.llm.base_url, profile.llm.api_key, model
            )
            if not facts:
                return 0
            existing = {
                m["content"].strip().lower() for m in await memory_store.list(profile.name)
            }
            added = 0
            for fact in facts:
                if fact.strip().lower() in existing:
                    continue
                await memory_store.add(profile.name, fact, source_session_id=session_id)
                existing.add(fact.strip().lower())
                added += 1
            if added:
                logger.info("memory: added %d facts for profile %s", added, profile.name)
            return added
        except Exception as exc:  # noqa: BLE001 - never break session teardown
            logger.warning("extract_and_upsert failed for %s: %s", session_id, exc)
            return 0


memory_extractor = MemoryExtractor()
