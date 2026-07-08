from app.services.system_config import SystemConfigStore


def test_default_when_empty(tmp_path):
    s = SystemConfigStore(str(tmp_path / "system_config.json"))
    assert s.get().base_context == ""


def test_set_persists_across_instances(tmp_path):
    p = str(tmp_path / "system_config.json")
    SystemConfigStore(p).set_base_context("hello")
    assert SystemConfigStore(p).get().base_context == "hello"


def test_imports_legacy_and_keeps_file(tmp_path):
    from app.services.system_config import SystemConfig

    p = tmp_path / "system_config.json"
    p.write_text(SystemConfig(base_context="seeded").model_dump_json())
    s = SystemConfigStore(str(p))
    assert s.get().base_context == "seeded"
    assert p.exists()  # legacy file kept as backup, never deleted


def test_malformed_legacy_file_falls_back_to_defaults(tmp_path, caplog):
    import logging

    p = tmp_path / "system_config.json"
    p.write_text("{not valid json")
    with caplog.at_level(logging.WARNING):
        s = SystemConfigStore(str(p))
        assert s.get().base_context == ""
    assert p.exists()


def test_honors_settings_path_set_after_construction(tmp_path, monkeypatch):
    """Same singleton-timing hazard as the keyed stores: system_config_store
    is constructed once at import time, so _ensure() must re-read
    settings.system_config_path lazily rather than a value captured eagerly."""
    from app.core.settings import settings
    from app.services.system_config import SystemConfig, SystemConfigStore

    # constructed the same way the real singleton is (settings_attr, no explicit
    # path) and BEFORE the monkeypatch, like the real module-level singleton
    store = SystemConfigStore(settings_attr="system_config_path")

    seeded = tmp_path / "system_config.json"
    seeded.write_text(SystemConfig(base_context="from-settings-path").model_dump_json())
    monkeypatch.setattr(settings, "system_config_path", str(seeded))

    assert store.get().base_context == "from-settings-path"


def test_never_falls_back_to_real_default_path(tmp_path, monkeypatch):
    from app.core.settings import settings
    from app.services.system_config import SystemConfigStore

    store = SystemConfigStore(settings_attr="system_config_path")
    monkeypatch.setattr(settings, "system_config_path", str(tmp_path / "nonexistent.json"))
    assert store.get().base_context == ""
