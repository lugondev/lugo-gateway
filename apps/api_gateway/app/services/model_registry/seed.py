"""Idempotent startup seed: registers every model the STT registries and
installed TTS engines already know about, so an admin can toggle enabled/stage
on them without having to hand-enter every one first. Never overwrites an
existing entry (an admin's enabled/stage edit on a previously-seeded row must
survive a re-seed on the next boot)."""

from __future__ import annotations

from app.services.model_registry.store import model_registry_store
from app.services.stt.model_registry import STT_MODEL_REGISTRIES
from app.services.tts.service import tts_service


async def seed_known_models() -> None:
    for engine, registry in STT_MODEL_REGISTRIES.items():
        for m in registry.list_models():
            if await model_registry_store.find("stt", engine, m["id"]) is None:
                await model_registry_store.create("stt", engine, m["id"], m["label"])
    for engine_name in tts_service.providers:
        if await model_registry_store.find("tts", engine_name, engine_name) is None:
            await model_registry_store.create("tts", engine_name, engine_name, engine_name)
