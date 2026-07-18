"""Threshold-triggered compaction of raw memory facts into a structured
user-profile document.

One LLM call rebuilds the profile from (current doc + all buffered facts);
the folded facts are then pruned so the buffer stays small. Best-effort: any
failure is logged and swallowed, and facts are pruned only after the doc write
succeeds and only for the ids captured at compaction start (concurrently added
facts survive).
"""

from __future__ import annotations

import logging

import httpx

from app.services.memory.retriever import MAX_DOC_CHARS, _truncate_at_boundary
from app.services.memory.store import memory_store, profile_doc_store
from app.services.profiles.models import Profile
from app.services.system_config import system_config_store

logger = logging.getLogger(__name__)

COMPACTION_PROMPT = (
    "You maintain a compact profile of a user, written in the user's language. "
    "You are given the CURRENT PROFILE (may be empty) and a list of NEW FACTS. "
    "Return the updated profile as Markdown, starting with '## User Profile' and "
    "using these sub-sections, omitting any that would be empty:\n"
    "### Danh tính\n### Dự án\n### Sở thích\n### Ràng buộc\n### Quan hệ\n"
    "Merge duplicates, keep bullets short, and when two facts conflict prefer the "
    "more recent one (facts are listed oldest first). Keep the ENTIRE profile "
    "under about 1500 characters total. Return ONLY the Markdown."
)


class MemoryCompactor:
    def _model(self, profile: Profile) -> str:
        return profile.memory.extractor_model or profile.llm.model

    async def _call_llm(
        self, profile: Profile, current_doc: str, facts: list[str]
    ) -> str:
        prompt = (
            "CURRENT PROFILE:\n"
            + (current_doc or "(empty)")
            + "\n\nNEW FACTS (oldest first):\n"
            + "\n".join(f"- {f}" for f in facts)
        )
        headers = (
            {"Authorization": f"Bearer {profile.llm.api_key}"}
            if profile.llm.api_key
            else {}
        )
        async with httpx.AsyncClient(
            timeout=system_config_store.get().conversation.llm_timeout_seconds
        ) as client:
            resp = await client.post(
                f"{profile.llm.base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json={
                    "model": self._model(profile),
                    "messages": [
                        {"role": "system", "content": COMPACTION_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
        return str(content).strip()

    async def maybe_compact(self, profile: Profile, user_id: str | None = None) -> bool:
        """Compact iff the raw buffer has reached the profile's threshold."""
        try:
            if not profile.memory.enabled or not profile.llm.base_url:
                return False
            items = await memory_store.list(profile.name, user_id=user_id)
            threshold = max(1, profile.memory.compaction_threshold)
            if len(items) < threshold and len(items) < profile.memory.max_facts:
                return False
            return await self.compact(profile, user_id=user_id, items=items)
        except Exception as exc:  # noqa: BLE001 - compaction is best-effort
            logger.warning("maybe_compact failed for %s: %s", profile.name, exc)
            return False

    async def compact(
        self, profile: Profile, user_id: str | None = None, items: list[dict] | None = None
    ) -> bool:
        if items is None:
            items = await memory_store.list(profile.name, user_id=user_id)
        if not items:
            return False
        # oldest first so the LLM can honor "prefer the more recent fact"
        items = sorted(items, key=lambda i: (i["created_at"] or "", i["id"]))
        fact_ids = [i["id"] for i in items]
        facts = [i["content"] for i in items]
        current = await profile_doc_store.get(profile.name, user_id=user_id)
        current_doc = current["content"] if current else ""
        new_doc = (await self._call_llm(profile, current_doc, facts) or "").strip()
        if not new_doc:
            logger.warning(
                "compaction produced empty doc for %s; keeping facts", profile.name
            )
            return False
        new_doc = _truncate_at_boundary(new_doc, MAX_DOC_CHARS)
        await profile_doc_store.upsert(profile.name, new_doc, user_id=user_id)
        await memory_store.delete_many(fact_ids)
        logger.info(
            "memory: compacted %d facts into profile %s", len(fact_ids), profile.name
        )
        return True


memory_compactor = MemoryCompactor()
