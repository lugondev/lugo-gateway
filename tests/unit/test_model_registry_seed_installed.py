import pytest
from app.services.model_registry.seed import seed_installed_models_to_registry
from app.services.model_registry.store import model_registry_store


async def test_seeds_cached_whisper_models(monkeypatch):
    from app.services.whisper_models import whisper_manager
    monkeypatch.setattr(whisper_manager, "snapshot", lambda: {
        "models": [
            {"size": "tiny", "label": "Tiny", "cached": True, "active": True},
            {"size": "large-v3", "label": "Large", "cached": False, "active": False},
        ],
        "active": "tiny",
    })
    from app.services.models import model_manager
    monkeypatch.setattr(model_manager, "snapshot", lambda: {"installed": [], "active": None})

    await seed_installed_models_to_registry()
    assert await model_registry_store.find("stt", "whisper", "tiny") is not None
    assert await model_registry_store.find("stt", "whisper", "large-v3") is None  # not cached


async def test_idempotent_and_preserves_disabled(monkeypatch):
    from app.services.whisper_models import whisper_manager
    from app.services.models import model_manager
    monkeypatch.setattr(whisper_manager, "snapshot", lambda: {
        "models": [{"size": "tiny", "label": "Tiny", "cached": True, "active": True}], "active": "tiny"})
    monkeypatch.setattr(model_manager, "snapshot", lambda: {"installed": [], "active": None})

    await model_registry_store.create("stt", "whisper", "tiny", "Tiny", enabled=False)
    await seed_installed_models_to_registry()
    # ensure_registry_entry re-enables a disabled row — acceptable: it IS installed.
    entry = await model_registry_store.find("stt", "whisper", "tiny")
    assert entry["enabled"] is True
    await seed_installed_models_to_registry()  # second run: no crash, still one row
    all_tiny = [e for e in await model_registry_store.list_all()
                if e["engine"] == "whisper" and e["model_id"] == "tiny"]
    assert len(all_tiny) == 1
