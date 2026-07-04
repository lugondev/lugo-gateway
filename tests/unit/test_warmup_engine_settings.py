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
