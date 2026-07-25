"""What happens to the profiles that pinned a Model Registry row when that row
is removed from service: their binding is cleared, so they fall back to the
server default instead of a row that's gone (or switched off).

Blanking, not repointing: nothing can guess which other row the admin would have
chosen, and an empty binding already means "inherit the default" everywhere it's
read (see SttConfig/LlmConfig's field docs and check_model_allowed's
short-circuit).

Called from the admin routes only (delete_entry / update_entry) -- deliberately
NOT from model_registry_store, which the startup migrations also mutate
(migrate_drop_stale_tts_engine_shims deletes rows on every boot). Cascading from
the store would make each boot rewrite profile config.
"""

from __future__ import annotations


async def clear_bindings_for(kind: str, engine: str, model_id: str) -> list[str]:
    """Clear every profile binding pinning exactly this (kind, engine, model_id).
    Returns a label per binding cleared -- empty when nothing pinned the row.

    Exact-match only: a binding with a blank engine or a blank model pins no row
    (it's the inherit-the-default case), so no row's removal may rewrite it.
    """
    from app.services.profiles.store import profile_store
    from app.services.tts.profile_store import tts_profile_store

    if not engine or not model_id:
        return []

    cleared: list[str] = []

    if kind == "stt":
        for profile in list(profile_store.list().values()):
            if (profile.stt.engine, profile.stt.model) == (engine, model_id):
                profile_store.upsert(profile.model_copy(update={
                    "stt": profile.stt.model_copy(update={"engine": "", "model": ""}),
                }))
                cleared.append(f"{profile.name} (stt)")

    elif kind == "llm":
        for profile in list(profile_store.list().values()):
            if (profile.llm.engine, profile.llm.model) == (engine, model_id):
                profile_store.upsert(profile.model_copy(update={
                    "llm": profile.llm.model_copy(update={"engine": "", "model": ""}),
                }))
                cleared.append(f"{profile.name} (llm)")

    elif kind == "tts":
        # TTS profiles carry the binding themselves (engine + model_id); the
        # chatllm profile only names a TTS profile, so it needs no rewrite.
        for tts_profile in list(tts_profile_store.list().values()):
            if (tts_profile.engine, tts_profile.model_id) == (engine, model_id):
                tts_profile_store.upsert(
                    tts_profile.model_copy(update={"engine": "", "model_id": ""}))
                cleared.append(f"{tts_profile.name} (tts profile)")

    return cleared
