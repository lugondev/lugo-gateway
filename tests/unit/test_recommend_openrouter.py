from app.services.recommend.capabilities import detect_capabilities
from app.services.recommend.catalog import CANDIDATES
from app.services.recommend.service import _augment_config_flags


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


def test_augment_config_flags_does_not_set_a_system_wide_openrouter_flag():
    """No system-wide OpenRouter key anymore -- availability is per Model
    Registry entry (see test_stt_service_openrouter.py's has_key_for_engine
    coverage), not a global toggle. Capabilities.has() defaults an unset
    module flag to False, so the "openrouter" requirement on the qwen3_asr_or/
    whisper_or candidates above still resolves sensibly (not pre-configured)
    even though _augment_config_flags never sets it."""
    caps = detect_capabilities()
    _augment_config_flags(caps)
    assert "openrouter" not in caps.modules
    assert caps.has("openrouter") is False
