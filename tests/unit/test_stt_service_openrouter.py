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


def test_reinit_remote_providers_rebuilds_whisper_service_and_eventlab():
    from app.services.system_config import RemoteSttConfig
    from app.services.stt.providers.remote_whisper_provider import RemoteWhisperProvider

    svc = STTService()
    original_whisper_service = svc.providers["whisper_service"]
    original_eventlab = svc.providers["eventlab"]

    new_cfg = RemoteSttConfig(
        whisper_service_base_url="https://new-endpoint.example/v1",
        whisper_service_api_key="new-key",
        whisper_service_model="whisper-2",
        eventlab_base_url="https://eventlab.example/v1",
        eventlab_api_key="ev-key",
        eventlab_model="whisper-1",
        remote_stt_timeout_seconds=15.0,
    )
    svc.reinit_remote_providers(new_cfg)

    assert svc.providers["whisper_service"] is not original_whisper_service
    assert isinstance(svc.providers["whisper_service"], RemoteWhisperProvider)
    assert svc.providers["whisper_service"].base_url == "https://new-endpoint.example/v1"
    assert svc.providers["whisper_service"].api_key == "new-key"
    assert svc.providers["whisper_service"].model == "whisper-2"
    assert svc.providers["whisper_service"].timeout_seconds == 15.0

    assert svc.providers["eventlab"] is not original_eventlab
    assert svc.providers["eventlab"].base_url == "https://eventlab.example/v1"
