from app.services.stt.profile import STT_PROFILES, resolve_stt_profile


def test_vietnamese_profile_uses_qwen3_asr():
    # Benchmark (FLEURS vi) showed qwen3_asr beats PhoWhisper on VN; vi now uses it.
    assert resolve_stt_profile("vi") == ("qwen3_asr", "vi")


def test_english_profile_uses_qwen3_asr():
    assert resolve_stt_profile("en") == ("qwen3_asr", "en")


def test_multilingual_profile_auto_detects():
    assert resolve_stt_profile("multi") == ("qwen3_asr", None)


def test_en_vi_profile_auto_detects_for_code_switching():
    assert resolve_stt_profile("en_vi") == ("qwen3_asr", None)


def test_case_insensitive_and_trimmed():
    assert resolve_stt_profile("  VI ") == ("qwen3_asr", "vi")


def test_empty_or_unknown_returns_none():
    assert resolve_stt_profile("") is None
    assert resolve_stt_profile(None) is None
    assert resolve_stt_profile("klingon") is None


def test_profiles_registry_lists_all_keys():
    assert set(STT_PROFILES) == {"vi", "en", "multi", "en_vi"}
