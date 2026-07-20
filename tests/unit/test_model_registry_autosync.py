from app.services.model_registry.autosync import ensure_registry_entry, disable_registry_entry
from app.services.model_registry.store import model_registry_store


async def test_ensure_creates_enabled_entry():
    await ensure_registry_entry("stt", "whisper", "tiny", "whisper — Tiny")
    entry = await model_registry_store.find("stt", "whisper", "tiny")
    assert entry["enabled"] is True and entry["stage"] == "stable" and entry["label"] == "whisper — Tiny"


async def test_ensure_reenables_without_clobbering_config():
    await model_registry_store.create(
        "stt", "whisper", "tiny", "Custom label", enabled=False,
        api_key="sk-secret", config={"beam_size": 7},
    )
    await ensure_registry_entry("stt", "whisper", "tiny", "whisper — Tiny")
    entry = await model_registry_store.find("stt", "whisper", "tiny")
    assert entry["enabled"] is True
    assert entry["label"] == "Custom label"        # not overwritten
    assert entry["api_key"] == "sk-secret"          # preserved
    assert entry["config"] == {"beam_size": 7}      # preserved


async def test_disable_keeps_row():
    await model_registry_store.create("stt", "whisper", "tiny", "Tiny", enabled=True, api_key="k")
    await disable_registry_entry("stt", "whisper", "tiny")
    entry = await model_registry_store.find("stt", "whisper", "tiny")
    assert entry is not None and entry["enabled"] is False and entry["api_key"] == "k"
