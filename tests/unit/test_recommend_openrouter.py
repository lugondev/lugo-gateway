from app.services.recommend.capabilities import detect_capabilities
from app.services.recommend.catalog import CANDIDATES
from app.services.recommend.service import _augment_config_flags
from app.services.system_config import SystemConfigStore


def test_catalog_has_openrouter_stt_candidates():
    ids = {c.id for c in CANDIDATES if c.category == "stt"}
    assert "qwen3_asr_or" in ids
    assert "whisper_or" in ids

    by_id = {c.id: c for c in CANDIDATES}
    for cid in ("qwen3_asr_or", "whisper_or"):
        c = by_id[cid]
        assert c.requires == ["openrouter"]
        assert c.action["kind"] == "config"
        assert c.size_gb is None


def test_augment_config_flags_unconfigured(tmp_path, monkeypatch):
    fresh = SystemConfigStore(str(tmp_path / "system_config.json"))
    monkeypatch.setattr("app.services.recommend.service.system_config_store", fresh)
    caps = detect_capabilities()
    _augment_config_flags(caps)
    assert caps.modules["openrouter"] is False


def test_augment_config_flags_configured(tmp_path, monkeypatch):
    fresh = SystemConfigStore(str(tmp_path / "system_config.json"))
    fresh.set_openrouter_api_key("sk-or-test")
    monkeypatch.setattr("app.services.recommend.service.system_config_store", fresh)
    caps = detect_capabilities()
    _augment_config_flags(caps)
    assert caps.modules["openrouter"] is True
