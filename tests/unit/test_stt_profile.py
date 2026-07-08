import pytest

from app.core.settings import settings
from app.services.profiles.models import Profile, SttConfig
from app.services.stt.profile import STT_PROFILES, resolve_stt, resolve_stt_profile


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


# --- resolve_stt: profile-driven STT resolution -----------------------------

@pytest.fixture
def _server_default(monkeypatch):
    # Pin the server-wide default so tests don't depend on the ambient .env.
    monkeypatch.setattr(settings, "stt_profile", "")
    monkeypatch.setattr(settings, "conversation_stt_engine", "whisper")
    monkeypatch.setattr(settings, "conversation_language", "vi")
    monkeypatch.setattr(settings, "default_stt_engine", "vosk")


def test_resolve_stt_no_profile_uses_server_default(_server_default):
    assert resolve_stt(None) == ("whisper", "vi")


def test_resolve_stt_profile_preset_wins_over_server_default(_server_default):
    p = Profile(name="p", stt=SttConfig(profile="vi"))
    assert resolve_stt(p) == ("qwen3_asr", "vi")


def test_resolve_stt_preset_auto_detect_language_is_authoritative(_server_default):
    # A "multi" preset means auto-detect (None) — it must NOT fall back to the
    # server's conversation_language.
    p = Profile(name="p", stt=SttConfig(profile="multi"))
    assert resolve_stt(p) == ("qwen3_asr", None)


def test_resolve_stt_explicit_engine_language_override_preset(_server_default):
    p = Profile(name="p", stt=SttConfig(profile="vi", engine="whisper_mlx", language="en"))
    assert resolve_stt(p) == ("whisper_mlx", "en")


def test_resolve_stt_query_param_wins_over_profile(_server_default):
    p = Profile(name="p", stt=SttConfig(profile="vi"))
    assert resolve_stt(p, q_engine="vosk", q_language="fr") == ("vosk", "fr")


def test_resolve_stt_server_stt_profile_default_applies_without_profile(monkeypatch):
    monkeypatch.setattr(settings, "stt_profile", "en")
    monkeypatch.setattr(settings, "conversation_stt_engine", "whisper")
    assert resolve_stt(None) == ("qwen3_asr", "en")
