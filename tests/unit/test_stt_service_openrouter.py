import pytest

from app.services.model_registry.store import model_registry_store
from app.services.stt.providers.openrouter_provider import OpenRouterSttProvider
from app.services.stt.service import STTService


def test_providers_include_openrouter_engines():
    svc = STTService()
    assert isinstance(svc.providers["qwen3_asr_or"], OpenRouterSttProvider)
    assert svc.providers["qwen3_asr_or"].model == "qwen/qwen3-asr-flash-2026-02-10"
    assert isinstance(svc.providers["whisper_or"], OpenRouterSttProvider)
    assert svc.providers["whisper_or"].model == "openai/whisper-large-v3-turbo"


@pytest.mark.asyncio
async def test_list_engines_reports_unconfigured_openrouter():
    svc = STTService()
    entries = {e["engine"]: e for e in await svc.list_engines()}
    assert entries["qwen3_asr_or"]["mode"] == "remote"
    assert entries["qwen3_asr_or"]["available"] is False
    assert entries["whisper_or"]["mode"] == "remote"
    assert entries["whisper_or"]["available"] is False


@pytest.mark.asyncio
async def test_list_engines_reports_configured_openrouter_independently_per_engine():
    """Per-model keys: configuring qwen3_asr_or's entry must not also mark
    whisper_or as available -- each engine's "configured" state now comes
    from its own Model Registry entries, not one shared system-wide flag."""
    await model_registry_store.create(
        "stt", "qwen3_asr_or", "qwen/qwen3-asr-flash-2026-02-10", "Qwen3 ASR Flash", api_key="sk-or-test"
    )
    svc = STTService()
    entries = {e["engine"]: e for e in await svc.list_engines()}
    assert entries["qwen3_asr_or"]["available"] is True
    assert entries["qwen3_asr_or"]["detail"] == "qwen/qwen3-asr-flash-2026-02-10"
    assert entries["whisper_or"]["available"] is False


@pytest.mark.asyncio
async def test_reinit_remote_providers_rebuilds_whisper_service_and_eventlab():
    from app.services.stt.providers.remote_whisper_provider import RemoteWhisperProvider

    svc = STTService()
    original_whisper_service = svc.providers["whisper_service"]
    original_eventlab = svc.providers["eventlab"]

    # Rebuild reads fresh from the Model Registry now, so seed the entries
    # it'll resolve instead of handing it a RemoteSttConfig directly.
    await model_registry_store.create(
        "stt", "whisper_service", "whisper-2", "Whisper Service",
        base_url="https://new-endpoint.example/v1", api_key="new-key",
        config={"timeout_seconds": 15.0},
    )
    await model_registry_store.create(
        "stt", "eventlab", "whisper-1", "Eventlab",
        base_url="https://eventlab.example/v1", api_key="ev-key",
    )

    svc.reinit_remote_providers()

    assert svc.providers["whisper_service"] is not original_whisper_service
    assert isinstance(svc.providers["whisper_service"], RemoteWhisperProvider)
    assert svc.providers["whisper_service"].base_url == "https://new-endpoint.example/v1"
    assert svc.providers["whisper_service"].api_key == "new-key"
    assert svc.providers["whisper_service"].model == "whisper-2"
    assert svc.providers["whisper_service"].timeout_seconds == 15.0

    assert svc.providers["eventlab"] is not original_eventlab
    assert svc.providers["eventlab"].base_url == "https://eventlab.example/v1"
