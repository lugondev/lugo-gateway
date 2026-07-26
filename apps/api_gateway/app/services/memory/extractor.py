"""Post-session memory extraction (mem0-inspired).

One LLM call over the session transcript -> JSON array of durable user facts
-> deduped upsert into MemoryStore. Failures are logged and swallowed: memory
extraction must never break a session teardown.
"""

from __future__ import annotations

import json
import logging

import httpx

from app.services.history.store import session_store
from app.services.memory.compactor import memory_compactor
from app.services.memory.embedder import cosine, embed_texts_with_usage
from app.services.memory.store import memory_store
from app.services.profiles.models import Profile
from app.services.system_config import system_config_store
from app.services.usage.recorder import record_usage

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
        self, messages: list[dict], base_url: str, api_key: str, model: str,
        *, user_id: str = "", profile_id: str = "", engine: str = "",
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
                timeout=system_config_store.get().conversation.llm_timeout_seconds
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
                body = resp.json()
                content = body["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001 - extraction is best-effort
            logger.warning("memory extraction LLM call failed: %s", exc)
            return []
        # This is a real billable LLM call: without this row, post-session
        # memory work is spend that never shows up in usage/cost at all.
        try:
            usage = body.get("usage") or {}
            prompt_tokens = usage.get("prompt_tokens")
            completion_tokens = usage.get("completion_tokens")
            await record_usage(
                user_id=user_id, profile_id=profile_id, kind="llm", engine=engine,
                model_id=model, unit="tokens",
                native_amount=(prompt_tokens or 0) + (completion_tokens or 0),
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            )
        except Exception as exc:  # noqa: BLE001 - metering must never break extraction
            logger.warning("memory extraction usage metering failed: %s", exc)
        return _parse_facts(str(content))

    async def _maybe_embed(
        self, profile: Profile, texts: list[str], user_id: str | None = None
    ) -> list[list[float] | None]:
        """Embed texts when an embed_model is configured; else all None. Best-effort."""
        if not texts or not profile.memory.embed_model or not profile.llm.base_url:
            return [None] * len(texts)
        try:
            vecs, tokens = await embed_texts_with_usage(
                texts, profile.llm.base_url, profile.llm.api_key,
                profile.memory.embed_model,
            )
        except Exception as exc:  # noqa: BLE001 - embedding is best-effort
            logger.warning("memory embed failed: %s", exc)
            return [None] * len(texts)
        try:
            await record_usage(
                user_id=user_id or "", profile_id=profile.name, kind="embed",
                engine=profile.llm.engine or "", model_id=profile.memory.embed_model,
                unit="tokens", native_amount=tokens, prompt_tokens=tokens,
            )
        except Exception as exc:  # noqa: BLE001 - metering must never break extraction
            logger.warning("memory embed metering failed: %s", exc)
        if len(vecs) != len(texts):
            logger.warning(
                "memory embed length mismatch: got %d vectors for %d texts; "
                "storing facts without embeddings instead of dropping any",
                len(vecs), len(texts),
            )
            return [None] * len(texts)
        return vecs

    async def _quota_blocked(
        self, profile: Profile, model: str, user_id: str | None
    ) -> bool:
        """True when an applicable quota is already over its limit. Resolving
        provider_id is wrapped separately so a registry hiccup degrades to
        user/global-scope enforcement rather than blocking or crashing."""
        from app.services.model_registry.store import model_registry_store
        from app.services.quota.gate import QuotaExceededError, quota_gate

        provider_id = ""
        try:
            engine = profile.llm.engine or ""
            if engine:
                entry = await model_registry_store.find("llm", engine, model)
                provider_id = (entry or {}).get("config", {}).get("provider_id", "") if entry else ""
        except Exception:  # noqa: BLE001 - never block memory on a lookup
            provider_id = ""
        try:
            await quota_gate(user_id=user_id or "", provider_id=provider_id)
        except QuotaExceededError as exc:
            logger.warning("memory extraction skipped for %s: %s", profile.name, exc)
            return True
        except Exception as exc:  # noqa: BLE001 - fail-open, same as quota_gate itself
            logger.warning("memory quota check failed open for %s: %s", profile.name, exc)
        return False

    async def extract_and_upsert(
        self, session_id: str, profile: Profile, user_id: str | None = None
    ) -> int:
        """Extract facts from a finished session into the profile's memory."""
        try:
            if not profile.memory.enabled or not profile.llm.base_url:
                return 0
            messages = await session_store.get_messages(session_id)
            if len(messages) < 2:
                return 0
            model = profile.memory.extractor_model or profile.llm.model
            # Post-session memory work is real provider spend, so it goes
            # through the same gate as a turn -- but nobody is waiting on it, so
            # over-quota means "skip and log", never an error to a caller.
            if await self._quota_blocked(profile, model, user_id):
                return 0
            facts = await self.extract(
                messages, profile.llm.base_url, profile.llm.api_key, model,
                user_id=user_id or "", profile_id=profile.name,
                engine=profile.llm.engine or "",
            )
            if not facts:
                return 0
            existing_items = await memory_store.list(profile.name, user_id=user_id)
            existing_norm = {m["content"].strip().lower() for m in existing_items}
            existing_vecs = [
                m["embedding"] for m in existing_items if m.get("embedding")
            ]
            new_vecs = await self._maybe_embed(profile, facts, user_id=user_id)
            threshold = profile.memory.dedup_threshold
            added = 0
            for fact, vec in zip(facts, new_vecs):
                norm = fact.strip().lower()
                if norm in existing_norm:
                    continue
                if vec is not None and any(
                    cosine(vec, ev) >= threshold for ev in existing_vecs
                ):
                    continue
                await memory_store.add(
                    profile.name, fact, source_session_id=session_id, embedding=vec,
                    user_id=user_id,
                )
                existing_norm.add(norm)
                if vec is not None:
                    existing_vecs.append(vec)
                added += 1
            if added:
                logger.info("memory: added %d facts for profile %s", added, profile.name)
            await memory_compactor.maybe_compact(profile, user_id=user_id)
            return added
        except Exception as exc:  # noqa: BLE001 - never break session teardown
            logger.warning("extract_and_upsert failed for %s: %s", session_id, exc)
            return 0


memory_extractor = MemoryExtractor()
