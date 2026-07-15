import pytest

from app.services.model_registry.resolve import (
    resolve_omnivoice_config,
    resolve_remote_stt_config,
    resolve_stt_local_device,
)
from app.services.model_registry.store import model_registry_store
from app.services.system_config import OmnivoiceConfig, RemoteSttConfig


def test_resolve_stt_local_device_defaults_when_no_entry():
    assert resolve_stt_local_device("whisper_local") == {"device": "", "compute_type": "int8"}


@pytest.mark.asyncio
async def test_resolve_stt_local_device_reads_registry_config():
    await model_registry_store.create(
        "stt", "whisper_local", "", "Whisper Local",
        config={"device": "cuda", "compute_type": "float16"},
    )
    assert resolve_stt_local_device("whisper_local") == {"device": "cuda", "compute_type": "float16"}


def test_resolve_omnivoice_config_defaults_when_no_entry():
    assert resolve_omnivoice_config() == OmnivoiceConfig()


@pytest.mark.asyncio
async def test_resolve_omnivoice_config_reads_registry_entry():
    await model_registry_store.create(
        "tts", "omnivoice", "k2-fsa/OmniVoice-custom", "OmniVoice",
        config={"omnivoice_device": "mps", "omnivoice_dtype": "bfloat16"},
    )
    cfg = resolve_omnivoice_config()
    assert cfg.omnivoice_model_id == "k2-fsa/OmniVoice-custom"
    assert cfg.omnivoice_device == "mps"
    assert cfg.omnivoice_dtype == "bfloat16"
    assert cfg.omnivoice_server_host == "127.0.0.1"  # untouched default


def test_resolve_remote_stt_config_defaults_when_no_entries():
    assert resolve_remote_stt_config() == RemoteSttConfig()


@pytest.mark.asyncio
async def test_resolve_remote_stt_config_reads_both_registry_entries():
    await model_registry_store.create(
        "stt", "whisper_service", "gpt-4o-transcribe", "Whisper Service",
        base_url="https://api.example.com/v1", api_key="sk-abc",
        config={"timeout_seconds": 90.0},
    )
    await model_registry_store.create(
        "stt", "eventlab", "whisper-1", "Eventlab",
        base_url="https://eventlab.example.com", api_key="sk-def",
    )
    cfg = resolve_remote_stt_config()
    assert cfg.whisper_service_base_url == "https://api.example.com/v1"
    assert cfg.whisper_service_api_key == "sk-abc"
    assert cfg.whisper_service_model == "gpt-4o-transcribe"
    assert cfg.eventlab_base_url == "https://eventlab.example.com"
    assert cfg.eventlab_api_key == "sk-def"
    assert cfg.remote_stt_timeout_seconds == 90.0
