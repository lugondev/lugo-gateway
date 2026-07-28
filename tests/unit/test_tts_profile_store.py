import json

import pytest

from app.services.db.config_models import TtsProfileRow
from app.services.db.sync_engine import init_config_tables, session_scope
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


def _write_raw_row(name: str, data: str) -> None:
    """Write straight to the config_tts_profiles table via the sync engine,
    bypassing TtsProfileStore.upsert() (and therefore TtsProfile's own
    validation) entirely -- the same as data that arrived from a legacy
    import, a manual DB edit, or a row saved under an older code version."""
    init_config_tables()
    with session_scope() as s:
        s.merge(TtsProfileRow(name=name, data=data))


def test_malformed_stored_row_does_not_break_other_profiles(store):
    """SqliteBackedStore._ensure() (config_store.py) used to deserialize every
    row in one dict comprehension with no per-row guard, and left the cache
    at None on failure -- so ONE malformed row raised out of _ensure() and
    then made every subsequent list()/get() call, for every OTHER (perfectly
    fine) row, raise too, forever. `name` is TtsProfile's only field with no
    default, so a row missing it is a real model_validate_json failure,
    independent of the ref_audio_path-specific scenario below."""
    store.upsert(TtsProfile(name="good", engine="vieneu"))
    _write_raw_row("bad", json.dumps({"engine": "vieneu"}))  # missing required "name"
    store.invalidate()  # force the next call through _ensure()'s DB reload

    profiles = store.list()
    assert "good" in profiles
    assert profiles["good"].engine == "vieneu"
    assert "bad" not in profiles
    assert store.get("good") is not None
    assert store.get("good").engine == "vieneu"


def test_out_of_bounds_ref_audio_path_row_still_loads_and_serves_other_rows(store):
    """The specific landmine this regression guards: three rows in the live
    DB store an absolute path (`<repo>/artifacts/refs/*.wav`) that only
    satisfies containment when the process's CWD matches the exact host root
    that wrote it -- any deployment-root change flips them from valid to
    store-bricking. TtsProfile.ref_audio_path is deliberately NOT validated
    (see profile_models.py's module docstring) so this kind of row loads
    fine regardless of containment; the actual security boundary is
    TTSRequest.ref_audio_path (schemas/tts.py) at synthesis time, which
    still rejects a bad value on every read -- see
    test_ref_audio_path_containment.py and
    test_session_bad_ref_audio_path_degrades.py."""
    store.upsert(TtsProfile(name="good", engine="vieneu"))
    bad_data = json.dumps({
        "name": "legacy-bad", "owner_id": None, "engine": "omnivoice", "model_id": "",
        "voice_mode": "clone", "voice": "", "ref_audio_path": "/etc/passwd",
        "ref_text": "x", "instruct": "", "speed": None, "language": None,
    })
    _write_raw_row("legacy-bad", bad_data)
    store.invalidate()

    profiles = store.list()
    assert "good" in profiles
    assert "legacy-bad" in profiles
    assert profiles["legacy-bad"].ref_audio_path == "/etc/passwd"
    assert store.get("good").engine == "vieneu"
