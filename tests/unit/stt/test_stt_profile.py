import pytest

from app.services.profiles.models import Profile, SttConfig
from app.services.stt.profile import resolve_stt
from app.services.system_config import SystemConfigStore


# --- resolve_stt: profile-driven STT resolution -----------------------------

@pytest.fixture
def _server_default(monkeypatch, tmp_path):
    # Pin the server-wide default so tests don't depend on the ambient config DB.
    fresh = SystemConfigStore(str(tmp_path / "system_config.json"))
    fresh.set(
        fresh.get().model_copy(
            update={
                "engines": fresh.get().engines.model_copy(update={"default_stt_engine": "whisper"}),
                "conversation": fresh.get().conversation.model_copy(
                    update={"conversation_language": "vi"}
                ),
            }
        )
    )
    monkeypatch.setattr("app.services.system_config.system_config_store", fresh)


def test_resolve_stt_no_profile_uses_server_default(_server_default):
    assert resolve_stt(None) == ("whisper", "vi", "")


def test_resolve_stt_profile_engine_and_language_win_over_server_default(_server_default):
    p = Profile(name="p", stt=SttConfig(engine="qwen3_asr", language="en"))
    assert resolve_stt(p) == ("qwen3_asr", "en", "")


def test_resolve_stt_profile_engine_only_keeps_server_language(_server_default):
    p = Profile(name="p", stt=SttConfig(engine="qwen3_asr"))
    assert resolve_stt(p) == ("qwen3_asr", "vi", "")


def test_resolve_stt_query_language_wins_over_profile(_server_default):
    p = Profile(name="p", stt=SttConfig(engine="qwen3_asr", language="vi"))
    assert resolve_stt(p, q_language="fr") == ("qwen3_asr", "fr", "")


def test_resolve_stt_engines_default_when_no_profile(monkeypatch, tmp_path):
    fresh = SystemConfigStore(str(tmp_path / "system_config.json"))
    fresh.set(
        fresh.get().model_copy(
            update={
                "engines": fresh.get().engines.model_copy(update={"default_stt_engine": "vosk"}),
                "conversation": fresh.get().conversation.model_copy(
                    update={"conversation_language": ""}
                ),
            }
        )
    )
    monkeypatch.setattr("app.services.system_config.system_config_store", fresh)
    # No profile -> engines.default_stt_engine; empty conversation_language -> None (auto-detect).
    assert resolve_stt(None) == ("vosk", None, "")


def test_resolve_stt_model_from_profile(_server_default):
    # No explicit language on the SttConfig, so language falls back to the
    # server default (conversation_language="vi" per _server_default).
    p = Profile(name="p", stt=SttConfig(engine="qwen3_asr", model="1.7b"))
    assert resolve_stt(p) == ("qwen3_asr", "vi", "1.7b")


def test_resolve_stt_model_query_param_wins_over_profile(_server_default):
    p = Profile(name="p", stt=SttConfig(engine="qwen3_asr", model="1.7b"))
    assert resolve_stt(p, q_model="0.6b") == ("qwen3_asr", "vi", "0.6b")


def test_resolve_stt_model_defaults_empty_when_unset(_server_default):
    assert resolve_stt(None) == ("whisper", "vi", "")
