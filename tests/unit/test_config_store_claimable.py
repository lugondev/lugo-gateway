"""Adversarial regression for H4: a stored row that fails model_validate_json
(e.g. a validator added after the row was written) still holds its primary
key. Before the fix, get(name) returning None for such a row was
indistinguishable from the name being genuinely free, so:

  - POST create saw no 409 and silently overwrote the row (owner_id becomes
    the attacker's).
  - PUT (upsert-or-create) skipped `_can_write` entirely, because `existing`
    was None, and fell through to the same silent overwrite.
  - seed_default_servers() replaced a malformed preset row with fresh
    defaults on every restart (data loss).

The fix: SqliteBackedStore tracks skipped keys (`_unreadable`) and exposes
`exists(name)`, which routes/seed now consult instead of `get(name) is not
None`. These tests write the malformed row straight to the DB (bypassing
upsert()/model validation entirely -- the same gap that hid the original
bug) and round-trip it through _ensure() via a fresh store instance, exactly
like the `_write_raw_row` pattern in test_tts_profile_store.py.
"""
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.db.config_models import McpServerRow, TtsProfileRow
from app.services.db.sync_engine import init_config_tables, session_scope
from app.services.mcp.presets import seed_default_servers
from app.services.mcp.server_store import McpServerStore
from app.services.tts.profile_store import TtsProfileStore


def _write_raw_tts_row(name: str, data: str) -> None:
    """Straight to the config_tts_profiles table, bypassing
    TtsProfileStore.upsert() (and TtsProfile's own validation) entirely --
    see test_tts_profile_store.py's identically-named helper."""
    init_config_tables()
    with session_scope() as s:
        s.merge(TtsProfileRow(name=name, data=data))


def _write_raw_mcp_row(name: str, data: str) -> None:
    init_config_tables()
    with session_scope() as s:
        s.merge(McpServerRow(name=name, data=data))


def _read_raw_tts_row(name: str) -> str | None:
    with session_scope() as s:
        row = s.get(TtsProfileRow, name)
        return row.data if row is not None else None


def _read_raw_mcp_row(name: str) -> str | None:
    with session_scope() as s:
        row = s.get(McpServerRow, name)
        return row.data if row is not None else None


MALFORMED_TTS_DATA = json.dumps({"engine": "vieneu"})  # missing required "name"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_tts_store(tmp_path, monkeypatch):
    """Same pattern as test_tts_profile_routes.py: a fresh TtsProfileStore
    instance (no cache primed by another test / the app lifespan) wired into
    the route module. It still talks to the same per-test tmp DB the
    `_tmp_db` conftest fixture configured (session_scope() reads the
    globally-configured engine, independent of this store's own `path`,
    which is only used for the one-time legacy-JSON import)."""
    fresh = TtsProfileStore(str(tmp_path / "tts_profiles.json"))
    monkeypatch.setattr("app.api.routes.tts_profiles.tts_profile_store", fresh)
    return fresh


# ---------------------------------------------------------------------------
# Store-level: the skip must be preserved, and exist() must catch what get()
# can't tell.
# ---------------------------------------------------------------------------


def test_malformed_row_get_returns_none_but_exists_is_true(tmp_path):
    _write_raw_tts_row("victim", MALFORMED_TTS_DATA)
    store = TtsProfileStore(str(tmp_path / "unused.json"))

    assert store.get("victim") is None  # the skip is preserved
    assert store.exists("victim") is True  # but the name is NOT free


def test_genuinely_absent_name_does_not_exist(tmp_path):
    store = TtsProfileStore(str(tmp_path / "unused.json"))
    assert store.get("nobody-home") is None
    assert store.exists("nobody-home") is False


# ---------------------------------------------------------------------------
# HTTP-level: POST must not be able to claim/overwrite an unreadable row.
# ---------------------------------------------------------------------------


def test_post_create_over_unreadable_row_is_409_not_overwrite(client):
    _write_raw_tts_row("victim", MALFORMED_TTS_DATA)

    resp = client.post("/v1/tts/profiles", json={"name": "victim", "engine": "vieneu"})

    assert resp.status_code == 409
    # The raw row is byte-identical afterwards -- NOT overwritten with the
    # attacker's data (which would have made them the owner).
    assert _read_raw_tts_row("victim") == MALFORMED_TTS_DATA


def test_post_create_genuinely_free_name_still_works(client):
    """The fix must not break the normal case: a name with no row at all
    (readable or not) still creates fine."""
    resp = client.post("/v1/tts/profiles", json={"name": "brand-new", "engine": "vieneu"})

    assert resp.status_code == 200
    assert resp.json()["data"]["name"] == "brand-new"


def test_put_on_unreadable_row_is_a_clear_error_not_silent_create_over(client):
    _write_raw_tts_row("victim-put", MALFORMED_TTS_DATA)

    resp = client.put(
        "/v1/tts/profiles/victim-put",
        json={"name": "victim-put", "engine": "vieneu"},
    )

    # Never a silent 200 upsert-or-create -- PUT's existing/_can_write branch
    # must never be skipped just because get() returned None.
    assert resp.status_code != 200
    assert resp.status_code in (409, 500)
    assert _read_raw_tts_row("victim-put") == MALFORMED_TTS_DATA


def test_delete_on_unreadable_row_does_not_silently_succeed(client):
    _write_raw_tts_row("victim-del", MALFORMED_TTS_DATA)

    resp = client.delete("/v1/tts/profiles/victim-del")

    assert resp.status_code != 200
    assert _read_raw_tts_row("victim-del") == MALFORMED_TTS_DATA


# ---------------------------------------------------------------------------
# seed_default_servers must not replace a malformed preset row with defaults.
# ---------------------------------------------------------------------------


def test_seed_default_servers_does_not_overwrite_malformed_preset_row(tmp_path):
    # A row for the "basic-tools" preset name that fails McpServer's own
    # url-scheme validator (see models.py's field_validator) -- realistic:
    # this is exactly the kind of row a validator added after the fact would
    # newly reject.
    malformed = json.dumps({
        "name": "basic-tools", "owner_id": None, "url": "ftp://localhost:8090",
        "headers": {}, "enabled": False,
    })
    _write_raw_mcp_row("basic-tools", malformed)
    store = McpServerStore(str(tmp_path / "mcp_servers.json"))

    seed_default_servers(store)

    assert _read_raw_mcp_row("basic-tools") == malformed
    assert store.get("basic-tools") is None  # still unreadable, as before
    assert store.exists("basic-tools") is True


def test_seed_default_servers_still_seeds_a_genuinely_absent_preset(tmp_path):
    store = McpServerStore(str(tmp_path / "mcp_servers.json"))
    seed_default_servers(store)
    assert store.get("basic-tools") is not None
