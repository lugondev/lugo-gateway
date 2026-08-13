import json
import logging

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


def test_imports_legacy_json_and_keeps_file(tmp_path):
    p = tmp_path / "profiles.json"
    p.write_text(json.dumps({"profiles": {"seed": Profile(name="seed").model_dump()}}))
    s = ProfileStore(str(p))
    assert s.get("seed").name == "seed"   # imported into the DB
    assert p.exists()                      # legacy file kept as backup, never deleted


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


def test_tts_imports_legacy_json_and_keeps_file(tmp_path):
    p = tmp_path / "tts_profiles.json"
    p.write_text(json.dumps({"profiles": {"seed": TtsProfile(name="seed").model_dump()}}))
    s = TtsProfileStore(str(p))
    assert s.get("seed").name == "seed"   # imported into the DB
    assert p.exists()                      # legacy file kept as backup, never deleted


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


def test_mcp_imports_legacy_json_and_keeps_file(tmp_path):
    p = tmp_path / "mcp_servers.json"
    p.write_text(json.dumps({"servers": {"seed": McpServer(name="seed", url="http://seed").model_dump()}}))
    s = McpServerStore(str(p))
    assert s.get("seed").name == "seed"   # imported into the DB
    assert p.exists()                      # legacy file kept as backup, never deleted


def test_mcp_no_reimport_when_table_has_rows(tmp_path):
    p = tmp_path / "mcp_servers.json"
    McpServerStore(str(p)).upsert(McpServer(name="live", url="http://live"))   # table now has a row; p never created
    p.write_text(json.dumps({"servers": {"stale": McpServer(name="stale", url="http://stale").model_dump()}}))
    s = McpServerStore(str(p))
    assert s.get("live") is not None
    assert s.get("stale") is None          # not imported (table already had data)
    assert p.exists()                      # left alone (no import happened)


def test_malformed_record_is_skipped_good_records_still_import(tmp_path, caplog):
    p = tmp_path / "profiles.json"
    p.write_text(
        json.dumps(
            {
                "profiles": {
                    "good": Profile(name="good").model_dump(),
                    "bad": {"name": "bad", "llm": "not-a-valid-llm-config"},
                }
            }
        )
    )
    with caplog.at_level(logging.WARNING):
        s = ProfileStore(str(p))
        assert s.get("good").name == "good"   # good record imported
        assert s.get("bad") is None            # malformed record skipped, not dropped-whole-import
    assert p.exists()                          # file kept regardless
    assert any("bad" in rec.message for rec in caplog.records)


def test_mcp_malformed_record_is_skipped(tmp_path):
    p = tmp_path / "mcp_servers.json"
    p.write_text(
        json.dumps(
            {
                "servers": {
                    "good": McpServer(name="good", url="http://good").model_dump(),
                    "bad": {"name": "bad"},  # missing required "url"
                }
            }
        )
    )
    s = McpServerStore(str(p))
    assert s.get("good").name == "good"
    assert s.get("bad") is None
    assert p.exists()


def test_store_honors_settings_path_set_after_construction(tmp_path, monkeypatch):
    """Simulates the real singleton bug: `profile_store` is constructed once
    at module-import time, before any test fixture gets a chance to
    monkeypatch settings. _ensure() must re-read settings.profiles_path
    lazily (at load time), not a value resolved at construction — otherwise
    monkeypatching settings in conftest never actually redirects the
    singleton away from the real ./profiles.json."""
    import app.services.profiles.store as store_module
    from app.core.settings import settings

    store = store_module.ProfileStore()  # constructed BEFORE the monkeypatch, like the real singleton

    seeded = tmp_path / "profiles.json"
    seeded.write_text(json.dumps({"profiles": {"seeded": Profile(name="seeded").model_dump()}}))
    monkeypatch.setattr(settings, "profiles_path", str(seeded))

    assert store.get("seeded") is not None  # reads the path monkeypatched after construction


def test_store_never_falls_back_to_real_default_path(tmp_path, monkeypatch):
    """Once settings is repointed at a nonexistent tmp path, the store must
    not fall back to the real default (./profiles.json) even though that was
    the value in effect when the module was imported."""
    import app.services.profiles.store as store_module
    from app.core.settings import settings

    store = store_module.ProfileStore()
    monkeypatch.setattr(settings, "profiles_path", str(tmp_path / "nonexistent.json"))
    assert store.list() == {}
