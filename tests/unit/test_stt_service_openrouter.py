from app.services.stt.providers.openrouter_provider import OpenRouterSttProvider
from app.services.stt.service import STTService
from app.services.system_config import SystemConfigStore


def test_providers_include_openrouter_engines():
    svc = STTService()
    assert isinstance(svc.providers["qwen3_asr_or"], OpenRouterSttProvider)
    assert svc.providers["qwen3_asr_or"].model == "qwen/qwen3-asr-flash-2026-02-10"
    assert isinstance(svc.providers["whisper_or"], OpenRouterSttProvider)
    assert svc.providers["whisper_or"].model == "openai/whisper-large-v3-turbo"


def test_list_engines_reports_unconfigured_openrouter(tmp_path, monkeypatch):
    fresh = SystemConfigStore(str(tmp_path / "system_config.json"))
    monkeypatch.setattr("app.services.stt.service.system_config_store", fresh)
    svc = STTService()
    entries = {e["engine"]: e for e in svc.list_engines()}
    assert entries["qwen3_asr_or"]["mode"] == "remote"
    assert entries["qwen3_asr_or"]["available"] is False
    assert entries["whisper_or"]["mode"] == "remote"
    assert entries["whisper_or"]["available"] is False


def test_list_engines_reports_configured_openrouter(tmp_path, monkeypatch):
    fresh = SystemConfigStore(str(tmp_path / "system_config.json"))
    fresh.set_openrouter_api_key("sk-or-test")
    monkeypatch.setattr("app.services.stt.service.system_config_store", fresh)
    svc = STTService()
    entries = {e["engine"]: e for e in svc.list_engines()}
    assert entries["qwen3_asr_or"]["available"] is True
    assert entries["qwen3_asr_or"]["detail"] == "qwen/qwen3-asr-flash-2026-02-10"
    assert entries["whisper_or"]["available"] is True
    assert entries["whisper_or"]["detail"] == "openai/whisper-large-v3-turbo"
