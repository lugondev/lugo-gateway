from __future__ import annotations

import uuid

from sqlalchemy import select

from app.services.db.engine import db_session
from app.services.db.models import ModelRegistryEntry


def _entry_dict(e: ModelRegistryEntry) -> dict:
    return {
        "id": e.id, "kind": e.kind, "engine": e.engine, "model_id": e.model_id,
        "label": e.label, "enabled": e.enabled, "stage": e.stage,
    }


class ModelRegistryStore:
    async def list_all(self) -> list[dict]:
        async with db_session() as s:
            rows = (
                await s.execute(select(ModelRegistryEntry).order_by(
                    ModelRegistryEntry.kind, ModelRegistryEntry.engine, ModelRegistryEntry.model_id
                ))
            ).scalars().all()
            return [_entry_dict(e) for e in rows]

    async def find(self, kind: str, engine: str, model_id: str) -> ModelRegistryEntry | None:
        async with db_session() as s:
            return (
                await s.execute(
                    select(ModelRegistryEntry).where(
                        ModelRegistryEntry.kind == kind,
                        ModelRegistryEntry.engine == engine,
                        ModelRegistryEntry.model_id == model_id,
                    )
                )
            ).scalar_one_or_none()

    async def create(self, kind: str, engine: str, model_id: str, label: str, stage: str = "stable") -> dict:
        async with db_session() as s:
            row = ModelRegistryEntry(
                id=str(uuid.uuid4()), kind=kind, engine=engine, model_id=model_id,
                label=label, enabled=True, stage=stage,
            )
            s.add(row)
            await s.commit()
            return _entry_dict(row)

    async def set_fields(self, entry_id: str, **fields) -> dict | None:
        async with db_session() as s:
            row = await s.get(ModelRegistryEntry, entry_id)
            if row is None:
                return None
            for key, value in fields.items():
                setattr(row, key, value)
            await s.commit()
            return _entry_dict(row)


model_registry_store = ModelRegistryStore()
