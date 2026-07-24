"""Regression: commit 949c3a7 renamed the OpenAI-compatible remote providers
openai_stt->http_stt and openai_tts->http_tts in code but left stored rows
referencing the dead names. Those rows then failed stt/tts_service
get_provider() with EngineNotFoundError -- the /stt/warm endpoint reported
"STT engine (server default) not ready" and device streams 500'd. The startup
migration migrate_renamed_engine_names() self-heals any DB (dev + prod) on
boot.
"""

import asyncio

import pytest

from app.services.db.engine import init_db
from app.services.model_registry.seed import migrate_renamed_engine_names
from app.services.model_registry.store import model_registry_store
from app.services.profiles.models import LlmConfig, Profile, SttConfig
from app.services.profiles.store import ProfileStore
from app.services.system_config import EngineDefaults, SystemConfig, SystemConfigStore
from app.services.tts.profile_models import TtsProfile
from app.services.tts.profile_store import TtsProfileStore


@pytest.fixture
def stores(tmp_path, monkeypatch):
    """Fresh, tmp-DB-backed store singletons.

    The migration imports the singletons inside the function body, so patching
    the module attributes here (not this test's own bindings) is what it reads.
    model_registry_store is already pointed at the per-test tmp DB and cache-
    invalidated by the autouse `_tmp_db` fixture, so it's used as-is.
    """
    asyncio.run(init_db())
    profiles = ProfileStore(str(tmp_path / "profiles.json"))
    tts = TtsProfileStore(str(tmp_path / "tts_profiles.json"))
    sysconf = SystemConfigStore(path=str(tmp_path / "system_config.json"))
    monkeypatch.setattr("app.services.profiles.store.profile_store", profiles)
    monkeypatch.setattr("app.services.tts.profile_store.tts_profile_store", tts)
    monkeypatch.setattr("app.services.system_config.system_config_store", sysconf)
    return profiles, tts, sysconf


def test_renames_across_registry_profiles_tts_and_defaults(stores):
    profiles, tts, sysconf = stores
    asyncio.run(model_registry_store.create("stt", "openai_stt", "Qwen/Qwen3-ASR-0.6B", "Qwen3-ASR"))
    asyncio.run(model_registry_store.create("tts", "openai_tts", "vieneu", "VieNeu"))
    profiles.upsert(Profile(name="p", stt=SttConfig(engine="openai_stt", model="Qwen/Qwen3-ASR-0.6B")))
    tts.upsert(TtsProfile(name="t", engine="openai_tts"))
    sysconf.set(SystemConfig(engines=EngineDefaults(default_stt_engine="openai_stt", default_tts_engine="openai_tts")))

    asyncio.run(migrate_renamed_engine_names())

    assert {e["engine"] for e in asyncio.run(model_registry_store.list_all())} == {"http_stt", "http_tts"}
    assert profiles.get("p").stt.engine == "http_stt"
    assert profiles.get("p").stt.model == "Qwen/Qwen3-ASR-0.6B"  # unrelated field preserved
    assert tts.get("t").engine == "http_tts"
    assert sysconf.get().engines.default_stt_engine == "http_stt"
    assert sysconf.get().engines.default_tts_engine == "http_tts"


def test_idempotent_and_leaves_other_engines_untouched(stores):
    profiles, tts, sysconf = stores
    asyncio.run(model_registry_store.create("stt", "qwen3_asr", "0.6b", "Q"))
    profiles.upsert(Profile(name="keep", stt=SttConfig(engine="vosk"), llm=LlmConfig(engine="OA")))
    profiles.upsert(Profile(name="old", stt=SttConfig(engine="openai_stt")))

    # Idempotent: running twice must not error and must leave stable values.
    asyncio.run(migrate_renamed_engine_names())
    asyncio.run(migrate_renamed_engine_names())

    assert profiles.get("keep").stt.engine == "vosk"       # unrelated STT engine untouched
    assert profiles.get("keep").llm.engine == "OA"         # LLM engine names are not part of the rename
    assert profiles.get("old").stt.engine == "http_stt"
    assert {e["engine"] for e in asyncio.run(model_registry_store.list_all())} == {"qwen3_asr"}


def test_noop_on_clean_db(stores):
    profiles, tts, sysconf = stores
    # Nothing to migrate: must not raise and must not invent rows/defaults.
    asyncio.run(migrate_renamed_engine_names())
    assert asyncio.run(model_registry_store.list_all()) == []
    assert profiles.list() == {}
    # A fresh SystemConfig keeps its schema defaults (not the renamed values).
    assert sysconf.get().engines.default_stt_engine == EngineDefaults().default_stt_engine
