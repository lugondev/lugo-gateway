"""The Lugo route must resolve STT from the chatllm profile (like the
conversation stream does), not just from server-wide settings — so a device
that sends only a profile id streams against that profile's STT."""

from app.core.settings import settings
from app.services.profiles.models import Profile, SttConfig
from app.services.profiles.store import ProfileStore

import app.api.routes.lugo as lugo


def test_lugo_resolve_uses_profile_stt_preset(monkeypatch, tmp_path):
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    fresh.upsert(Profile(name="dev", stt=SttConfig(profile="vi")))
    monkeypatch.setattr(lugo, "profile_store", fresh)

    _profile, stt_engine, language, _tts, _idle = lugo._resolve("dev")

    assert stt_engine == "qwen3_asr"
    assert language == "vi"


def test_lugo_resolve_explicit_engine_overrides_preset(monkeypatch, tmp_path):
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    fresh.upsert(Profile(name="dev2", stt=SttConfig(engine="whisper_mlx", language="en")))
    monkeypatch.setattr(lugo, "profile_store", fresh)

    _profile, stt_engine, language, _tts, _idle = lugo._resolve("dev2")

    assert stt_engine == "whisper_mlx"
    assert language == "en"


def test_lugo_resolve_no_profile_falls_back_to_settings(monkeypatch, tmp_path):
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    monkeypatch.setattr(lugo, "profile_store", fresh)
    monkeypatch.setattr(settings, "stt_profile", "")
    monkeypatch.setattr(settings, "conversation_stt_engine", "stub-fallback-stt")

    _profile, stt_engine, _language, _tts, _idle = lugo._resolve(None)

    assert stt_engine == "stub-fallback-stt"
