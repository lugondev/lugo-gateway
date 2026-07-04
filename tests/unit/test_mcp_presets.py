from app.services.mcp.models import McpServer
from app.services.mcp.presets import PRESET_SERVERS, seed_default_servers
from app.services.mcp.server_store import McpServerStore


def _store(tmp_path):
    return McpServerStore(str(tmp_path / "mcp.json"))


def test_seed_adds_preset_servers(tmp_path):
    store = _store(tmp_path)
    seed_default_servers(store)
    names = set(store.list().keys())
    assert names == {p.name for p in PRESET_SERVERS}


def test_seed_presets_start_disabled(tmp_path):
    store = _store(tmp_path)
    seed_default_servers(store)
    for preset in PRESET_SERVERS:
        assert store.get(preset.name).enabled is False


def test_seed_does_not_overwrite_existing_entry(tmp_path):
    store = _store(tmp_path)
    name = PRESET_SERVERS[0].name
    store.upsert(McpServer(name=name, url="http://custom", enabled=True))
    seed_default_servers(store)
    assert store.get(name).url == "http://custom"
    assert store.get(name).enabled is True


def test_seed_is_idempotent(tmp_path):
    store = _store(tmp_path)
    seed_default_servers(store)
    seed_default_servers(store)
    assert len(store.list()) == len(PRESET_SERVERS)
