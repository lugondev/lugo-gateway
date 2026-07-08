import json
from pathlib import Path

from app.services.mcp.models import McpServer
from app.services.mcp.server_store import McpServerStore
from app.services.profiles.models import Profile
from app.services.profiles.store import ProfileStore
from app.services.tts.profile_models import TtsProfile
from app.services.tts.profile_store import TtsProfileStore


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


def test_tts_crud_roundtrip_persists(tmp_path):
    s = TtsProfileStore(str(tmp_path / "tts_profiles.json"))
    s.upsert(TtsProfile(name="a"))
    assert "a" in s.list()
    assert s.get("a").name == "a"
    # a fresh instance sees it (persisted to the DB, not just cache)
    assert TtsProfileStore(str(tmp_path / "tts_profiles.json")).get("a") is not None
    s.delete("a")
    assert s.get("a") is None


def test_tts_imports_legacy_json_then_deletes_file(tmp_path):
    p = tmp_path / "tts_profiles.json"
    p.write_text(json.dumps({"profiles": {"seed": TtsProfile(name="seed").model_dump()}}))
    s = TtsProfileStore(str(p))
    assert s.get("seed").name == "seed"   # imported into the DB
    assert not p.exists()                  # legacy file removed after import


def test_tts_no_reimport_when_table_has_rows(tmp_path):
    p = tmp_path / "tts_profiles.json"
    TtsProfileStore(str(p)).upsert(TtsProfile(name="live"))   # table now has a row; p never created
    p.write_text(json.dumps({"profiles": {"stale": TtsProfile(name="stale").model_dump()}}))
    s = TtsProfileStore(str(p))
    assert s.get("live") is not None
    assert s.get("stale") is None          # not imported (table already had data)
    assert p.exists()                      # left alone (no import happened)


def test_mcp_crud_roundtrip_persists(tmp_path):
    s = McpServerStore(str(tmp_path / "mcp_servers.json"))
    s.upsert(McpServer(name="a", url="http://a"))
    assert "a" in s.list()
    assert s.get("a").name == "a"
    # a fresh instance sees it (persisted to the DB, not just cache)
    assert McpServerStore(str(tmp_path / "mcp_servers.json")).get("a") is not None
    s.delete("a")
    assert s.get("a") is None


def test_mcp_imports_legacy_json_then_deletes_file(tmp_path):
    p = tmp_path / "mcp_servers.json"
    p.write_text(json.dumps({"servers": {"seed": McpServer(name="seed", url="http://seed").model_dump()}}))
    s = McpServerStore(str(p))
    assert s.get("seed").name == "seed"   # imported into the DB
    assert not p.exists()                  # legacy file removed after import


def test_mcp_no_reimport_when_table_has_rows(tmp_path):
    p = tmp_path / "mcp_servers.json"
    McpServerStore(str(p)).upsert(McpServer(name="live", url="http://live"))   # table now has a row; p never created
    p.write_text(json.dumps({"servers": {"stale": McpServer(name="stale", url="http://stale").model_dump()}}))
    s = McpServerStore(str(p))
    assert s.get("live") is not None
    assert s.get("stale") is None          # not imported (table already had data)
    assert p.exists()                      # left alone (no import happened)
