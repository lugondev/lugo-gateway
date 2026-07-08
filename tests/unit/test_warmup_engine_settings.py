from app.core.settings import settings


def test_warmup_stt_engines_defaults_to_just_the_conversation_engine(monkeypatch):
    monkeypatch.setattr(settings, "conversation_stt_engine", "whisper")
    monkeypatch.setattr(settings, "extra_warmup_stt_engines", "")
    assert settings.warmup_stt_engines == ["whisper"]


def test_warmup_stt_engines_includes_extra_engines_a_device_pins(monkeypatch):
    monkeypatch.setattr(settings, "conversation_stt_engine", "whisper")
    monkeypatch.setattr(settings, "extra_warmup_stt_engines", "qwen3_asr")
    assert settings.warmup_stt_engines == ["whisper", "qwen3_asr"]


def test_warmup_stt_engines_dedupes_and_strips_whitespace(monkeypatch):
    monkeypatch.setattr(settings, "conversation_stt_engine", "whisper")
    monkeypatch.setattr(settings, "extra_warmup_stt_engines", " whisper, qwen3_asr ,qwen3_asr")
    assert settings.warmup_stt_engines == ["whisper", "qwen3_asr"]


def test_warmup_tts_engines_defaults_to_just_the_conversation_engine(monkeypatch):
    monkeypatch.setattr(settings, "conversation_tts_engine", "vieneu")
    monkeypatch.setattr(settings, "extra_warmup_tts_engines", "")
    assert settings.warmup_tts_engines == ["vieneu"]


def test_warmup_tts_engines_includes_extra_engines(monkeypatch):
    monkeypatch.setattr(settings, "conversation_tts_engine", "vieneu")
    monkeypatch.setattr(settings, "extra_warmup_tts_engines", "omnivoice")
    assert settings.warmup_tts_engines == ["vieneu", "omnivoice"]


# --- boot warm-up enumerates every profile / tts-profile engine ---
from app.services import warmup  # noqa: E402


class _FakeStore:
    def __init__(self, d):
        self._d = d

    def list(self):
        return self._d


def _fake_profile(stt_engine):
    stt = type("S", (), {"profile": "", "engine": stt_engine, "language": ""})()
    return type("P", (), {"stt": stt})()


def _fake_tts_profile(engine):
    return type("T", (), {"engine": engine})()


def test_boot_warmup_includes_profile_and_tts_profile_engines(monkeypatch):
    monkeypatch.setattr(settings, "conversation_stt_engine", "whisper")
    monkeypatch.setattr(settings, "extra_warmup_stt_engines", "")
    monkeypatch.setattr(settings, "conversation_tts_engine", "vieneu")
    monkeypatch.setattr(settings, "extra_warmup_tts_engines", "")
    monkeypatch.setattr(
        "app.services.profiles.store.profile_store",
        _FakeStore({"p": _fake_profile("qwen3_asr")}),
    )
    monkeypatch.setattr(
        "app.services.tts.profile_store.tts_profile_store",
        _FakeStore({"t": _fake_tts_profile("omnivoice")}),
    )
    stt, tts = warmup.engines_for_boot_warmup()
    assert "whisper" in stt and "qwen3_asr" in stt   # settings default + profile
    assert "vieneu" in tts and "omnivoice" in tts     # settings default + tts profile
    assert len(stt) == len(set(stt)) and len(tts) == len(set(tts))  # de-duplicated
