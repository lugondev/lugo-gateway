"""Bridge from the Models page (artifact lifecycle) to the Model Registry
(selection source of truth). Installing a local model ensures it has an
enabled registry entry so profiles can pick it; deleting disables that entry
(never removes the row, so an admin's api_key/config survives a reinstall)."""

from __future__ import annotations

from app.services.model_registry.store import model_registry_store


async def ensure_registry_entry(kind: str, engine: str, model_id: str, label: str) -> None:
    entry = await model_registry_store.find(kind, engine, model_id)
    if entry is None:
        await model_registry_store.create(kind, engine, model_id, label, enabled=True, stage="stable")
    elif not entry["enabled"]:
        await model_registry_store.set_fields(entry["id"], enabled=True)


async def disable_registry_entry(kind: str, engine: str, model_id: str) -> None:
    entry = await model_registry_store.find(kind, engine, model_id)
    if entry is not None:
        await model_registry_store.set_fields(entry["id"], enabled=False)
