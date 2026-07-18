import json

import pytest

from app.services.db.config_models import SystemRow
from app.services.db.sync_engine import session_scope
from app.services.model_registry.seed import (
    migrate_omnivoice_to_registry,
    migrate_remote_stt_to_registry,
    migrate_stt_local_device_to_registry,
    migrate_stt_local_models_to_registry,
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


def _set_raw_group(group: str, values: dict) -> None:
    """Write directly into a SystemRow's raw JSON, bypassing the current
    `SystemConfig` Pydantic schema -- needed here because Task 7 removed the
    `omnivoice`/`remote_stt` groups and 3 `stt_local` device fields from that
    schema entirely, so they're no longer reachable via
    `system_config_store.get().<group>`. `.get()` alone (unlike `.set()`)
    never writes a row through to the DB when none exists yet and there's no
    legacy JSON file to import -- it just caches a bare `SystemConfig()` in
    memory -- so `.set(.get())` is used here to force a real row onto disk
    before this function reads/writes it directly."""
    system_config_store.set(system_config_store.get())  # ensure the SystemRow exists on disk
    with session_scope() as s:
        row = s.get(SystemRow, 1)
        data = json.loads(row.data)
        data[group] = {**data.get(group, {}), **values}
        row.data = json.dumps(data)
    system_config_store._cache = None  # force reload from the DB row


@pytest.mark.asyncio
async def test_migrate_remote_stt_seeds_from_existing_config():
    _set_raw_group("remote_stt", {
        "whisper_service_base_url": "https://api.example.com",
        "whisper_service_api_key": "sk-old",
        "whisper_service_model": "whisper-1",
    })
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
    _set_raw_group("stt_local", {
        "whisper_local_device": "cuda", "whisper_local_compute_type": "float16",
        "qwen3_asr_device": "mps",
    })
    await migrate_stt_local_device_to_registry()
    whisper_entry = await model_registry_store.find("stt", "whisper_local", "")
    qwen_entry = await model_registry_store.find("stt", "qwen3_asr", "")
    assert whisper_entry["config"] == {"device": "cuda", "compute_type": "float16"}
    assert qwen_entry["config"] == {"device": "mps"}


@pytest.mark.asyncio
async def test_migrate_stt_local_device_not_shadowed_by_a_per_model_row():
    """Regression guard: a colliding enabled row under the same (kind, engine)
    -- e.g. one an admin adds -- must not shadow the migration's model_id=""
    config sentinel."""
    await model_registry_store.create(
        "stt", "whisper_local", "large-v3-turbo", "Whisper Large v3 Turbo", config={},
    )
    await model_registry_store.create(
        "stt", "qwen3_asr", "0.6b", "Qwen3-ASR 0.6B", config={},
    )
    _set_raw_group("stt_local", {
        "whisper_local_device": "cuda", "whisper_local_compute_type": "float16",
        "qwen3_asr_device": "mps",
    })
    await migrate_stt_local_device_to_registry()

    whisper_config_entry = await model_registry_store.find("stt", "whisper_local", "")
    qwen_config_entry = await model_registry_store.find("stt", "qwen3_asr", "")
    assert whisper_config_entry is not None, "migration was shadowed by the seed governance row"
    assert whisper_config_entry["config"] == {"device": "cuda", "compute_type": "float16"}
    assert qwen_config_entry is not None, "migration was shadowed by the seed governance row"
    assert qwen_config_entry["config"] == {"device": "mps"}


@pytest.mark.asyncio
async def test_migrate_stt_local_models_seeds_all_four_engines_from_legacy_values():
    _set_raw_group("stt_local", {
        "whisper_local_model": "large-v3",
        "whisper_vad_filter": False,
        "whisper_beam_size": 5,
        "whisper_condition_on_previous_text": True,
        "whisper_initial_prompt": "xin chào",
        "whisper_mlx_model_path": "models/stt/custom-mlx",
        "qwen3_asr_model": "Qwen/Qwen3-ASR-1.7B",
        "vosk_model_path": "models/stt/vosk-vn",
    })
    await migrate_stt_local_models_to_registry()

    whisper = await model_registry_store.find("stt", "whisper_local", "")
    assert whisper["config"] == {
        "default_model": "large-v3",
        "vad_filter": False,
        "beam_size": 5,
        "condition_on_previous_text": True,
        "initial_prompt": "xin chào",
    }
    mlx = await model_registry_store.find("stt", "whisper_mlx", "")
    assert mlx["config"] == {
        "model_path": "models/stt/custom-mlx",
        "condition_on_previous_text": True,
        "initial_prompt": "xin chào",
    }
    qwen = await model_registry_store.find("stt", "qwen3_asr", "")
    assert qwen["config"] == {"default_model": "Qwen/Qwen3-ASR-1.7B"}
    vosk = await model_registry_store.find("stt", "vosk", "")
    assert vosk["config"] == {"model_path": "models/stt/vosk-vn"}


@pytest.mark.asyncio
async def test_migrate_stt_local_models_seeds_defaults_when_no_legacy_values():
    await migrate_stt_local_models_to_registry()
    whisper = await model_registry_store.find("stt", "whisper_local", "")
    assert whisper["config"]["default_model"] == "large-v3-turbo"
    vosk = await model_registry_store.find("stt", "vosk", "")
    assert vosk["config"]["model_path"] == "models/stt/vosk-model-small-en-us-0.15"


@pytest.mark.asyncio
async def test_migrate_stt_local_models_adds_missing_keys_into_existing_sentinel_row():
    """The device migration already created stt/whisper_local/"" and
    stt/qwen3_asr/"" sentinel rows on existing installs -- this migration must
    ADD its keys into those rows, never replace them (device/compute_type must
    survive)."""
    await model_registry_store.create(
        "stt", "whisper_local", "", "Whisper Local (device/compute config)",
        config={"device": "cuda", "compute_type": "float16"},
    )
    _set_raw_group("stt_local", {"whisper_local_model": "large-v3"})
    await migrate_stt_local_models_to_registry()

    whisper = await model_registry_store.find("stt", "whisper_local", "")
    assert whisper["config"]["device"] == "cuda"
    assert whisper["config"]["compute_type"] == "float16"
    assert whisper["config"]["default_model"] == "large-v3"


@pytest.mark.asyncio
async def test_migrate_stt_local_models_never_overwrites_admin_edited_keys():
    """Idempotency: re-running the migration (every boot) must not clobber a
    value the admin has since edited in the Model Registry UI."""
    _set_raw_group("stt_local", {"whisper_local_model": "large-v3"})
    await migrate_stt_local_models_to_registry()
    entry = await model_registry_store.find("stt", "whisper_local", "")
    await model_registry_store.set_fields(
        entry["id"], config={**entry["config"], "default_model": "small"},
    )

    await migrate_stt_local_models_to_registry()

    entry = await model_registry_store.find("stt", "whisper_local", "")
    assert entry["config"]["default_model"] == "small"


@pytest.mark.asyncio
async def test_migrate_omnivoice_seeds_from_existing_config():
    _set_raw_group("omnivoice", {
        "omnivoice_device": "mps", "omnivoice_dtype": "bfloat16",
    })
    await migrate_omnivoice_to_registry()
    entry = await model_registry_store.find("tts", "omnivoice", "")
    assert entry is not None
    assert entry["model_id"] == ""
    assert entry["config"]["omnivoice_model_id"] == "k2-fsa/OmniVoice"  # default, kept in config
    assert entry["config"]["omnivoice_device"] == "mps"
    assert entry["config"]["omnivoice_dtype"] == "bfloat16"


@pytest.mark.asyncio
async def test_migrate_omnivoice_not_shadowed_by_a_placeholder_row():
    """Regression guard: a colliding enabled row under the same (kind, engine)
    -- e.g. one an admin adds -- must not shadow the migration's model_id=""
    config sentinel."""
    await model_registry_store.create("tts", "omnivoice", "omnivoice", "OmniVoice", config={})
    _set_raw_group("omnivoice", {"omnivoice_device": "mps", "omnivoice_dtype": "bfloat16"})

    await migrate_omnivoice_to_registry()

    config_entry = await model_registry_store.find("tts", "omnivoice", "")
    assert config_entry is not None, "migration was shadowed by the seed governance row"
    assert config_entry["config"]["omnivoice_device"] == "mps"
    assert config_entry["config"]["omnivoice_dtype"] == "bfloat16"
