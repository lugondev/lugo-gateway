"""Bridge from the Models page (artifact lifecycle) to the Model Registry
(selection source of truth). Installing a local model ensures it has an
enabled registry entry so profiles can pick it; deleting the artifact removes
its registry row -- unless the row carries admin-entered credentials
(api_key/base_url), in which case it's only disabled so a reinstall keeps
them. Local models (whisper/vosk/...) have no such secrets, so deleting them
cleans the row out entirely instead of leaving a dangling disabled entry."""

from __future__ import annotations

from app.services.model_registry.store import model_registry_store


async def ensure_registry_entry(kind: str, engine: str, model_id: str, label: str) -> None:
    entry = await model_registry_store.find(kind, engine, model_id)
    if entry is None:
        await model_registry_store.create(kind, engine, model_id, label, enabled=True, stage="stable")
    elif not entry["enabled"]:
        await model_registry_store.set_fields(entry["id"], enabled=True)


async def disable_registry_entry(kind: str, engine: str, model_id: str) -> None:
    """Called when a Models-page artifact is deleted. Removes the registry row
    so a deleted model doesn't linger as a dangling disabled entry -- but only
    when the row has nothing worth preserving. A row with an admin-entered
    api_key or base_url (service engines), or one linked to a provider (its
    own api_key/base_url are blank by design -- creds live on the provider),
    is kept and merely disabled, so a later reinstall doesn't force
    re-entering the credential or re-linking the provider."""
    entry = await model_registry_store.find(kind, engine, model_id)
    if entry is None:
        return
    provider_linked = bool((entry.get("config") or {}).get("provider_id"))
    if entry["api_key"] or entry["base_url"] or provider_linked:
        await model_registry_store.set_fields(entry["id"], enabled=False)
    else:
        await model_registry_store.delete(entry["id"])
