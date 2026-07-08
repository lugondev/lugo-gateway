import json
from pathlib import Path

from app.services.profiles.models import Profile
from app.services.profiles.store import ProfileStore


def test_crud_roundtrip_persists(tmp_path):
    s = ProfileStore(str(tmp_path / "profiles.json"))
    s.upsert(Profile(name="a"))
    assert "a" in s.list()
    assert s.get("a").name == "a"
    # a fresh instance sees it (persisted to the DB, not just cache)
    assert ProfileStore(str(tmp_path / "profiles.json")).get("a") is not None
    s.delete("a")
    assert s.get("a") is None


def test_imports_legacy_json_then_deletes_file(tmp_path):
    p = tmp_path / "profiles.json"
    p.write_text(json.dumps({"profiles": {"seed": Profile(name="seed").model_dump()}}))
    s = ProfileStore(str(p))
    assert s.get("seed").name == "seed"   # imported into the DB
    assert not p.exists()                  # legacy file removed after import


def test_no_reimport_when_table_has_rows(tmp_path):
    p = tmp_path / "profiles.json"
    ProfileStore(str(p)).upsert(Profile(name="live"))   # table now has a row; p never created
    p.write_text(json.dumps({"profiles": {"stale": Profile(name="stale").model_dump()}}))
    s = ProfileStore(str(p))
    assert s.get("live") is not None
    assert s.get("stale") is None          # not imported (table already had data)
    assert p.exists()                      # left alone (no import happened)
