"""Idempotent startup seed: registers every model the STT registries and
installed TTS engines already know about, so an admin can toggle enabled/stage
on them without having to hand-enter every one first. Never overwrites an
existing entry (an admin's enabled/stage edit on a previously-seeded row must
survive a re-seed on the next boot)."""

from __future__ import annotations

from app.services.model_registry.store import model_registry_store
from app.services.stt.model_registry import STT_MODEL_REGISTRIES
from app.services.system_config import system_config_store
from app.services.tts.service import tts_service


async def seed_known_models() -> None:
    for engine, registry in STT_MODEL_REGISTRIES.items():
        for m in registry.list_models():
            if await model_registry_store.find("stt", engine, m["id"]) is None:
                await model_registry_store.create("stt", engine, m["id"], m["label"])
    for engine_name in tts_service.providers:
        if await model_registry_store.find("tts", engine_name, engine_name) is None:
            await model_registry_store.create("tts", engine_name, engine_name, engine_name)


async def migrate_conversation_llm_to_registry() -> None:
    """One-time: the conversation LLM's base_url/api_key/model used to live in
    System settings (`SystemConfig.conversation_llm`, removed from the schema
    now that Model Registry `kind="llm"` entries are the only source). If an
    admin had already configured one and no `kind="llm"` entry is enabled yet,
    seed one from the raw, still-persisted JSON so upgrading never silently
    drops a working config. No-op once any `kind="llm"` entry is enabled --
    including a fresh install with nothing configured either way."""
    if await model_registry_store.find_enabled(kind="llm") is not None:
        return
    old = system_config_store.get_raw_group("conversation_llm")
    base_url = (old.get("conversation_llm_base_url") or "").strip()
    if not base_url:
        return
    await model_registry_store.create(
        kind="llm",
        engine="custom",
        model_id=old.get("conversation_llm_model") or "",
        label="Conversation LLM (migrated from System settings)",
        base_url=base_url,
        api_key=old.get("conversation_llm_api_key") or "",
    )
