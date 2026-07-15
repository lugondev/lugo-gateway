"""Idempotent startup seed: registers every model the STT registries and
installed TTS engines already know about, so an admin can toggle enabled/stage
on them without having to hand-enter every one first. Never overwrites an
existing entry (an admin's enabled/stage edit on a previously-seeded row must
survive a re-seed on the next boot)."""

from __future__ import annotations

from app.services.model_registry.store import model_registry_store
from app.services.stt.model_registry import STT_MODEL_REGISTRIES
from app.services.system_config import OmnivoiceConfig, RemoteSttConfig, system_config_store
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


async def migrate_remote_stt_to_registry() -> None:
    """One-time: whisper_service/eventlab used to live in
    SystemConfig.remote_stt -- removed from the schema (Task 7) in favor of
    Model Registry entries, so the legacy values (if any) are only reachable
    via the raw, still-persisted JSON group (see
    SystemConfigStore.get_raw_group). Seed a registry entry per engine from
    the current values if none is enabled yet for that engine -- no-op once
    migrated (including a fresh install with nothing configured)."""
    remote_stt = RemoteSttConfig(**system_config_store.get_raw_group("remote_stt"))
    if (
        await model_registry_store.find_enabled("stt", "whisper_service") is None
        and remote_stt.whisper_service_base_url.strip()
    ):
        await model_registry_store.create(
            "stt", "whisper_service", remote_stt.whisper_service_model,
            "Whisper Service (migrated from System settings)",
            base_url=remote_stt.whisper_service_base_url,
            api_key=remote_stt.whisper_service_api_key,
            config={"timeout_seconds": remote_stt.remote_stt_timeout_seconds},
        )
    if (
        await model_registry_store.find_enabled("stt", "eventlab") is None
        and remote_stt.eventlab_base_url.strip()
    ):
        await model_registry_store.create(
            "stt", "eventlab", remote_stt.eventlab_model,
            "Eventlab (migrated from System settings)",
            base_url=remote_stt.eventlab_base_url,
            api_key=remote_stt.eventlab_api_key,
            config={"timeout_seconds": remote_stt.remote_stt_timeout_seconds},
        )


async def migrate_stt_local_device_to_registry() -> None:
    """One-time: whisper_local/qwen3_asr device+compute_type used to live on
    SttLocalConfig -- 3 fields removed from that schema (Task 7) in favor of
    Model Registry entries, so the legacy values (if any) are only reachable
    via the raw, still-persisted JSON group (see
    SystemConfigStore.get_raw_group). Seed one engine-level registry entry
    each (model_id="" -- distinct from the per-size governance rows
    seed_known_models() already creates) from the current values. No-op once
    an enabled entry already exists for that engine."""
    stt_local_raw = system_config_store.get_raw_group("stt_local")
    if await model_registry_store.find_enabled("stt", "whisper_local") is None:
        await model_registry_store.create(
            "stt", "whisper_local", "", "Whisper Local (device/compute config)",
            config={
                "device": stt_local_raw.get("whisper_local_device", "cpu"),
                "compute_type": stt_local_raw.get("whisper_local_compute_type", "int8"),
            },
        )
    if await model_registry_store.find_enabled("stt", "qwen3_asr") is None:
        await model_registry_store.create(
            "stt", "qwen3_asr", "", "Qwen3-ASR (device config)",
            config={"device": stt_local_raw.get("qwen3_asr_device", "")},
        )


async def migrate_omnivoice_to_registry() -> None:
    """One-time: OmniVoice's whole config used to live in
    SystemConfig.omnivoice -- removed from the schema (Task 7) in favor of a
    single Model Registry entry, so the legacy values (if any) are only
    reachable via the raw, still-persisted JSON group (see
    SystemConfigStore.get_raw_group). No-op once an enabled tts/omnivoice
    entry already exists."""
    if await model_registry_store.find_enabled("tts", "omnivoice") is not None:
        return
    omnivoice = OmnivoiceConfig(**system_config_store.get_raw_group("omnivoice"))
    config = omnivoice.model_dump()
    config.pop("omnivoice_model_id")  # lives in the entry's model_id column, not config
    await model_registry_store.create(
        "tts", "omnivoice", omnivoice.omnivoice_model_id,
        "OmniVoice (migrated from System settings)",
        config=config,
    )
