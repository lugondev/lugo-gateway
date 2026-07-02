import pytest

from app.services.profiles.models import LlmConfig, Profile, TtsConfig
from app.services.profiles.store import ProfileStore


@pytest.fixture
def store(tmp_path):
    return ProfileStore(str(tmp_path / "profiles.json"))


def test_empty_store_returns_empty_dict(store):
    assert store.list() == {}


def test_upsert_and_get(store):
    p = Profile(name="test", system_prompt="Hello")
    store.upsert(p)
    result = store.get("test")
    assert result is not None
    assert result.system_prompt == "Hello"


def test_get_missing_returns_none(store):
    assert store.get("nonexistent") is None


def test_list_multiple_profiles(store):
    store.upsert(Profile(name="a"))
    store.upsert(Profile(name="b"))
    profiles = store.list()
    assert set(profiles.keys()) == {"a", "b"}


def test_upsert_overwrites_existing(store):
    store.upsert(Profile(name="x", system_prompt="old"))
    store.upsert(Profile(name="x", system_prompt="new"))
    assert store.get("x").system_prompt == "new"


def test_delete_removes_profile(store):
    store.upsert(Profile(name="del"))
    store.delete("del")
    assert store.get("del") is None


def test_delete_nonexistent_is_noop(store):
    store.delete("ghost")  # should not raise


def test_persists_across_instances(tmp_path):
    path = str(tmp_path / "profiles.json")
    s1 = ProfileStore(path)
    s1.upsert(Profile(name="persist", system_prompt="stay"))
    s2 = ProfileStore(path)
    assert s2.get("persist").system_prompt == "stay"


def test_auto_creates_file(tmp_path):
    store = ProfileStore(str(tmp_path / "new.json"))
    assert store.list() == {}


def test_profile_with_llm_and_tts_roundtrips(store):
    p = Profile(
        name="full",
        llm=LlmConfig(base_url="http://localhost:11434/v1", model="llama3.2"),
        tts=TtsConfig(engine="vieneu"),
        system_prompt="Be helpful.",
    )
    store.upsert(p)
    result = store.get("full")
    assert result.llm.model == "llama3.2"
    assert result.tts.engine == "vieneu"
