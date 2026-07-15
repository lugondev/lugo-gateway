import pytest

from app.services.model_registry.seed import (
    migrate_omnivoice_to_registry,
    migrate_remote_stt_to_registry,
    migrate_stt_local_device_to_registry,
)
from app.services.model_registry.store import model_registry_store
from app.services.system_config import system_config_store


@pytest.fixture(autouse=True)
def _reset_system_config_cache():
    """`system_config_store` is a process-global singleton whose in-memory
    cache (unlike `model_registry_store`'s, which conftest's `_tmp_db`
    explicitly invalidates per test) is never reset between tests. Some
    module (e.g. `app.services.tts.service`'s `TTSService.__init__`, pulled
    in transitively via `app.services.model_registry.seed`) calls
    `system_config_store.get()` at import time -- before `_tmp_db` points
    the DB engine at this test's tmp file -- which permanently seeds the
    cache from whatever engine existed at collection time. Without a reset
    here, `.set()` in these tests would populate the cache in memory but
    then fail writing through to the *current* per-test tmp DB, since its
    `config_system` table was never created against that stale engine.
    Reset before AND after so this test file never leaks a real/stale
    config into itself or other test modules."""
    system_config_store._cache = None
    yield
    system_config_store._cache = None


@pytest.mark.asyncio
async def test_migrate_remote_stt_seeds_from_existing_config():
    system_config_store.set(
        system_config_store.get().model_copy(update={
            "remote_stt": system_config_store.get().remote_stt.model_copy(update={
                "whisper_service_base_url": "https://api.example.com",
                "whisper_service_api_key": "sk-old",
                "whisper_service_model": "whisper-1",
            })
        })
    )
    await migrate_remote_stt_to_registry()
    entry = await model_registry_store.find_enabled("stt", "whisper_service")
    assert entry is not None
    assert entry["base_url"] == "https://api.example.com"
    assert entry["api_key"] == "sk-old"


@pytest.mark.asyncio
async def test_migrate_remote_stt_is_a_noop_once_migrated():
    await model_registry_store.create(
        "stt", "whisper_service", "whisper-1", "Whisper Service (manual)",
        base_url="https://manual.example.com",
    )
    await migrate_remote_stt_to_registry()
    entries = [e for e in await model_registry_store.list_all() if e["engine"] == "whisper_service"]
    assert len(entries) == 1
    assert entries[0]["base_url"] == "https://manual.example.com"


@pytest.mark.asyncio
async def test_migrate_stt_local_device_seeds_whisper_local_and_qwen3_asr():
    system_config_store.set(
        system_config_store.get().model_copy(update={
            "stt_local": system_config_store.get().stt_local.model_copy(update={
                "whisper_local_device": "cuda", "whisper_local_compute_type": "float16",
                "qwen3_asr_device": "mps",
            })
        })
    )
    await migrate_stt_local_device_to_registry()
    whisper_entry = await model_registry_store.find_enabled("stt", "whisper_local")
    qwen_entry = await model_registry_store.find_enabled("stt", "qwen3_asr")
    assert whisper_entry["config"] == {"device": "cuda", "compute_type": "float16"}
    assert qwen_entry["config"] == {"device": "mps"}


@pytest.mark.asyncio
async def test_migrate_omnivoice_seeds_from_existing_config():
    system_config_store.set(
        system_config_store.get().model_copy(update={
            "omnivoice": system_config_store.get().omnivoice.model_copy(update={
                "omnivoice_device": "mps", "omnivoice_dtype": "bfloat16",
            })
        })
    )
    await migrate_omnivoice_to_registry()
    entry = await model_registry_store.find_enabled("tts", "omnivoice")
    assert entry["model_id"] == "k2-fsa/OmniVoice"
    assert entry["config"]["omnivoice_device"] == "mps"
    assert entry["config"]["omnivoice_dtype"] == "bfloat16"
