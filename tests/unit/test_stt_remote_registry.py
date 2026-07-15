import pytest

from app.services.model_registry.store import model_registry_store
from app.services.stt.service import STTService


@pytest.mark.asyncio
async def test_stt_service_reads_remote_stt_from_registry():
    await model_registry_store.create(
        "stt", "whisper_service", "whisper-1", "Whisper Service",
        base_url="https://api.example.com", api_key="sk-abc",
    )
    service = STTService()
    provider = service.get_provider("whisper_service")
    assert provider.base_url == "https://api.example.com"
    assert provider.api_key == "sk-abc"
