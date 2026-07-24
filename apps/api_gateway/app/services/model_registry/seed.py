"""One-time migrations that back-fill config-carrying model_id="" sentinel
rows in the Model Registry from legacy SystemConfig fields. Each migration is
idempotent -- safe to run on every boot -- and never overwrites a value an
admin has since edited."""

from __future__ import annotations

from app.services.model_registry.store import model_registry_store
from app.services.system_config import OmnivoiceConfig, RemoteSttConfig, system_config_store


async def migrate_conversation_llm_to_registry() -> None:
    """One-time: the conversation LLM's base_url/api_key/model used to live in
    System settings (`SystemConfig.conversation_llm`, removed from the schema
    now that Model Registry `kind="llm"` entries are the only source). If an
    admin had already configured one and no `kind="llm"` entry is the default
    yet, seed one from the raw, still-persisted JSON so upgrading never
    silently drops a working config -- and mark it `is_default=True` since it
    represents what WAS the sole conversation LLM. No-op once any `kind="llm"`
    entry is already the default -- including a fresh install with nothing
    configured either way."""
    if await model_registry_store.find_default("llm") is not None:
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
        is_default=True,
    )


async def migrate_llm_default_flag() -> None:
    """One-time: before this feature, kind="llm" rows enforced enabled-exclusivity
    (at most one enabled at a time), and that lone enabled row WAS the
    conversation's active default -- find_enabled(kind="llm") resolved it
    directly. Exclusivity is now scoped to a separate is_default flag so
    multiple llm entries can be enabled (selectable per-profile) at once. On
    upgrade, promote whichever row was already enabled (there's at most one,
    by the old invariant) to is_default=True, so the conversation LLM doesn't
    silently go unset the first boot after this schema change. No-op once any
    llm row already has is_default=True (including a fresh install with
    nothing configured)."""
    if await model_registry_store.find_default("llm") is not None:
        return
    entry = await model_registry_store.find_enabled(kind="llm")
    if entry is not None:
        await model_registry_store.set_fields(entry["id"], is_default=True)


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
    each (model_id="" -- the engine-level config row, distinct from any
    per-model row an admin may add) from the current values. No-op once
    an enabled entry already exists for that engine."""
    stt_local_raw = system_config_store.get_raw_group("stt_local")
    if await model_registry_store.find("stt", "whisper_local", "") is None:
        await model_registry_store.create(
            "stt", "whisper_local", "", "Whisper Local (device/compute config)",
            config={
                "device": stt_local_raw.get("whisper_local_device", "cpu"),
                "compute_type": stt_local_raw.get("whisper_local_compute_type", "int8"),
            },
        )
    if await model_registry_store.find("stt", "qwen3_asr", "") is None:
        await model_registry_store.create(
            "stt", "qwen3_asr", "", "Qwen3-ASR (device config)",
            config={"device": stt_local_raw.get("qwen3_asr_device", "")},
        )


# New sentinel-config key -> the legacy SttLocalConfig field it migrates from.
_STT_LOCAL_LEGACY_FIELDS: dict[str, dict[str, str]] = {
    "whisper_local": {
        "default_model": "whisper_local_model",
        "vad_filter": "whisper_vad_filter",
        "beam_size": "whisper_beam_size",
        "condition_on_previous_text": "whisper_condition_on_previous_text",
        "initial_prompt": "whisper_initial_prompt",
    },
    "whisper_mlx": {
        "model_path": "whisper_mlx_model_path",
        "condition_on_previous_text": "whisper_condition_on_previous_text",
        "initial_prompt": "whisper_initial_prompt",
    },
    "qwen3_asr": {"default_model": "qwen3_asr_model"},
    # qwen3_asr_gguf has no legacy SttLocalConfig fields (it was born after the
    # sentinel-row migration). The legacy names below never exist in the raw
    # stt_local group, so raw.get() always falls through to
    # STT_ENGINE_CONFIG_DEFAULTS -- which is exactly how we want a brand-new
    # engine seeded: full default config, no migration source.
    "qwen3_asr_gguf": {
        "default_model": "qwen3_asr_gguf_model",
        "binary_path": "qwen3_asr_gguf_binary_path",
        "n_threads": "qwen3_asr_gguf_n_threads",
        "timeout_seconds": "qwen3_asr_gguf_timeout_seconds",
    },
    "vosk": {"model_path": "vosk_model_path"},
}

_STT_LOCAL_SENTINEL_LABELS = {
    "whisper_local": "Whisper Local (engine config)",
    "whisper_mlx": "Whisper MLX (engine config)",
    "qwen3_asr": "Qwen3-ASR (engine config)",
    "qwen3_asr_gguf": "Qwen3-ASR GGUF/CPU (engine config)",
    "vosk": "Vosk (engine config)",
}


async def migrate_stt_local_models_to_registry() -> None:
    """One-time-per-key: the per-engine default model and whisper decode
    tuning used to live on SttLocalConfig -- 8 fields removed from that
    schema in favor of the model_id="" sentinel rows' config, next to the
    device/compute_type keys migrate_stt_local_device_to_registry() already
    put there (so this must MERGE into an existing row, never replace it).
    Key-by-key: a key already present in the row's config (seeded on an
    earlier boot, or admin-edited since) is never overwritten, which is what
    makes re-running on every boot a no-op."""
    from app.services.model_registry.resolve import STT_ENGINE_CONFIG_DEFAULTS

    raw = system_config_store.get_raw_group("stt_local")
    for engine, fields in _STT_LOCAL_LEGACY_FIELDS.items():
        values = {
            key: raw.get(legacy_field, STT_ENGINE_CONFIG_DEFAULTS[engine][key])
            for key, legacy_field in fields.items()
        }
        entry = await model_registry_store.find("stt", engine, "")
        if entry is None:
            await model_registry_store.create(
                "stt", engine, "", _STT_LOCAL_SENTINEL_LABELS[engine], config=values,
            )
            continue
        merged = {**values, **(entry["config"] or {})}
        if merged != entry["config"]:
            await model_registry_store.set_fields(entry["id"], config=merged)


async def migrate_omnivoice_to_registry() -> None:
    """One-time: OmniVoice's whole config used to live in
    SystemConfig.omnivoice -- removed from the schema (Task 7) in favor of a
    single Model Registry entry, so the legacy values (if any) are only
    reachable via the raw, still-persisted JSON group (see
    SystemConfigStore.get_raw_group). Seeded under the reserved model_id=""
    sentinel (the config row; distinct from any tts/omnivoice restriction row
    an admin may add) -- resolve_omnivoice_config() reads
    omnivoice_model_id back out of `config`, so it must stay in there.
    No-op once a model_id="" entry already exists."""
    if await model_registry_store.find("tts", "omnivoice", "") is not None:
        return
    omnivoice = OmnivoiceConfig(**system_config_store.get_raw_group("omnivoice"))
    config = omnivoice.model_dump()
    await model_registry_store.create(
        "tts", "omnivoice", "",
        "OmniVoice (migrated from System settings)",
        config=config,
    )


async def seed_installed_models_to_registry() -> None:
    """Back-fill enabled entries for models that are already installed or already
    referenced by a profile, so flipping the gate to catalog-mode doesn't hide
    anything that currently works. Idempotent; never disables."""
    from app.services.model_registry.autosync import ensure_registry_entry
    from app.services.models import model_manager
    from app.services.profiles.store import profile_store
    from app.services.tts.profile_store import tts_profile_store
    from app.services.whisper_models import whisper_manager

    for m in whisper_manager.snapshot()["models"]:
        if m["cached"]:
            await ensure_registry_entry("stt", "whisper", m["size"], f"whisper — {m['label']}")

    for m in model_manager.snapshot()["installed"]:
        await ensure_registry_entry("stt", "vosk", m["name"], f"vosk — {m['name']}")

    for profile in profile_store.list().values():
        if profile.stt.engine and profile.stt.model:
            if await model_registry_store.find("stt", profile.stt.engine, profile.stt.model) is None:
                await ensure_registry_entry(
                    "stt", profile.stt.engine, profile.stt.model,
                    f"{profile.stt.engine} — {profile.stt.model} (in use)")
        if profile.llm.engine and profile.llm.model:
            if await model_registry_store.find("llm", profile.llm.engine, profile.llm.model) is None:
                await ensure_registry_entry(
                    "llm", profile.llm.engine, profile.llm.model,
                    f"{profile.llm.engine} — {profile.llm.model} (in use)")

    # TTS profiles gate on the (engine, engine) shim -- see check_model_allowed()
    # call in routes/tts_profiles.py, where profile.engine doubles as its own
    # model_id since TTS profiles don't select a separate model id. Nothing else
    # backfills this shape: migrate_omnivoice_to_registry() seeds a different,
    # unrelated model_id="" sentinel row, and engine_map's auto-sync keys rows by
    # real model ids, not by the engine name repeated as model_id. Without this,
    # catalog-mode's flip (Task 4) locks every existing TTS profile out of saving.
    for tts_profile in tts_profile_store.list().values():
        if tts_profile.engine:
            if await model_registry_store.find("tts", tts_profile.engine, tts_profile.engine) is None:
                await ensure_registry_entry(
                    "tts", tts_profile.engine, tts_profile.engine,
                    f"{tts_profile.engine} — {tts_profile.engine} (in use)")


# Provider renames that shipped in code but not in stored data. Commit 949c3a7
# renamed the OpenAI-compatible remote providers; every value below is a dead
# engine name that no longer resolves in stt_service/tts_service. Extend this
# map for any future provider rename.
_ENGINE_RENAMES = {
    "openai_stt": "http_stt",
    "openai_tts": "http_tts",
}


async def migrate_renamed_engine_names() -> None:
    """One-time, idempotent: rewrite dead engine names left in stored data by a
    code-side provider rename (commit 949c3a7: openai_stt->http_stt,
    openai_tts->http_tts).

    The rename touched the provider registration keys only; rows persisted
    before it -- Model Registry entries, chatllm profiles' SttConfig.engine, TTS
    profiles' engine, and the server-wide default engines -- kept the old names,
    so stt_service/tts_service.get_provider() raised EngineNotFoundError (the
    /stt/warm endpoint surfaced this as "STT engine (server default) not ready";
    device streams 500'd). Rewriting every stored occurrence to the current name
    self-heals any DB on boot. No-op once no old name remains, so it's safe to
    run on every startup.
    """
    from app.services.profiles.store import profile_store
    from app.services.system_config import system_config_store
    from app.services.tts.profile_store import tts_profile_store

    for entry in await model_registry_store.list_all():
        new = _ENGINE_RENAMES.get(entry["engine"])
        if new:
            await model_registry_store.set_fields(entry["id"], engine=new)

    for profile in profile_store.list().values():
        new = _ENGINE_RENAMES.get(profile.stt.engine)
        if new:
            profile_store.upsert(
                profile.model_copy(update={"stt": profile.stt.model_copy(update={"engine": new})})
            )

    for tts_profile in tts_profile_store.list().values():
        new = _ENGINE_RENAMES.get(tts_profile.engine)
        if new:
            tts_profile_store.upsert(tts_profile.model_copy(update={"engine": new}))

    cfg = system_config_store.get()
    engine_updates = {}
    new_stt = _ENGINE_RENAMES.get(cfg.engines.default_stt_engine)
    if new_stt:
        engine_updates["default_stt_engine"] = new_stt
    new_tts = _ENGINE_RENAMES.get(cfg.engines.default_tts_engine)
    if new_tts:
        engine_updates["default_tts_engine"] = new_tts
    if engine_updates:
        system_config_store.set(
            cfg.model_copy(update={"engines": cfg.engines.model_copy(update=engine_updates)})
        )
