import pytest

from app.services.tts.profile_models import TtsProfile
from app.services.tts.profile_store import TtsProfileStore


@pytest.fixture
def store(tmp_path):
    return TtsProfileStore(str(tmp_path / "tts_profiles.json"))


def test_empty_store_returns_empty_dict(store):
    assert store.list() == {}


def test_upsert_and_get(store):
    p = TtsProfile(name="test", engine="vieneu")
    store.upsert(p)
    result = store.get("test")
    assert result is not None
    assert result.engine == "vieneu"


def test_get_missing_returns_none(store):
    assert store.get("nonexistent") is None


def test_list_multiple_profiles(store):
    store.upsert(TtsProfile(name="a"))
    store.upsert(TtsProfile(name="b"))
    profiles = store.list()
    assert set(profiles.keys()) == {"a", "b"}


def test_upsert_overwrites_existing(store):
    store.upsert(TtsProfile(name="x", engine="vieneu"))
    store.upsert(TtsProfile(name="x", engine="omnivoice"))
    assert store.get("x").engine == "omnivoice"


def test_delete_removes_profile(store):
    store.upsert(TtsProfile(name="del"))
    store.delete("del")
    assert store.get("del") is None


def test_delete_nonexistent_is_noop(store):
    store.delete("ghost")  # should not raise


def test_persists_across_instances(tmp_path):
    path = str(tmp_path / "tts_profiles.json")
    s1 = TtsProfileStore(path)
    s1.upsert(TtsProfile(name="persist", engine="vieneu"))
    s2 = TtsProfileStore(path)
    assert s2.get("persist").engine == "vieneu"


def test_auto_creates_file(tmp_path):
    store = TtsProfileStore(str(tmp_path / "new.json"))
    assert store.list() == {}


def test_clone_profile_roundtrips(store):
    p = TtsProfile(
        name="cloned", engine="omnivoice", voice_mode="clone",
        ref_audio_path="artifacts/refs/host.wav", ref_text="hello",
    )
    store.upsert(p)
    result = store.get("cloned")
    assert result.voice_mode == "clone"
    assert result.ref_audio_path == "artifacts/refs/host.wav"
    assert result.ref_text == "hello"
