from app.services.system_config import SystemConfigStore


def test_default_when_empty(tmp_path):
    s = SystemConfigStore(str(tmp_path / "system_config.json"))
    assert s.get().base_context == ""


def test_set_persists_across_instances(tmp_path):
    p = str(tmp_path / "system_config.json")
    SystemConfigStore(p).set_base_context("hello")
    assert SystemConfigStore(p).get().base_context == "hello"


def test_imports_legacy_then_deletes(tmp_path):
    from app.services.system_config import SystemConfig

    p = tmp_path / "system_config.json"
    p.write_text(SystemConfig(base_context="seeded").model_dump_json())
    s = SystemConfigStore(str(p))
    assert s.get().base_context == "seeded"
    assert not p.exists()
