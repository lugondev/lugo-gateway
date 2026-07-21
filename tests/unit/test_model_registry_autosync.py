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


async def test_delete_keeps_row_with_credentials():
    # A service-engine row carrying an api_key is only disabled, so a reinstall
    # doesn't force re-entering the credential.
    await model_registry_store.create("stt", "whisper", "tiny", "Tiny", enabled=True, api_key="k")
    await disable_registry_entry("stt", "whisper", "tiny")
    entry = await model_registry_store.find("stt", "whisper", "tiny")
    assert entry is not None and entry["enabled"] is False and entry["api_key"] == "k"


async def test_delete_keeps_row_with_base_url():
    await model_registry_store.create(
        "stt", "whisper_service", "big", "Remote", enabled=True, base_url="https://x",
    )
    await disable_registry_entry("stt", "whisper_service", "big")
    entry = await model_registry_store.find("stt", "whisper_service", "big")
    assert entry is not None and entry["enabled"] is False and entry["base_url"] == "https://x"


async def test_delete_removes_local_row_without_credentials():
    # A local model (no api_key/base_url) has nothing worth preserving, so
    # deleting its artifact removes the registry row outright -- no dangling
    # disabled entry left behind.
    await model_registry_store.create("stt", "whisper", "tiny", "Tiny", enabled=True)
    await disable_registry_entry("stt", "whisper", "tiny")
    assert await model_registry_store.find("stt", "whisper", "tiny") is None


async def test_delete_missing_row_is_noop():
    await disable_registry_entry("stt", "whisper", "does-not-exist")
    assert await model_registry_store.find("stt", "whisper", "does-not-exist") is None
