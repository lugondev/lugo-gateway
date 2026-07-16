import pytest

from app.services.model_registry.resolve import (
    resolve_omnivoice_config,
    resolve_remote_stt_config,
    resolve_stt_engine_config,
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


@pytest.mark.asyncio
async def test_resolve_stt_local_device_ignores_seed_governance_row():
    """Regression guard: seed_known_models() creates one ENABLED row per known
    model size under the same (kind="stt", engine="whisper_local") pair (e.g.
    model_id="phowhisper-tiny", config={}) -- these must never shadow the
    real model_id="" config row. Before the fix, resolve_stt_local_device()
    used find_enabled_sync("stt", engine), which ignores model_id and would
    return whichever of these two rows the cache happened to iterate first."""
    await model_registry_store.create(
        "stt", "whisper_local", "phowhisper-tiny", "PhoWhisper Tiny", config={},
    )
    await model_registry_store.create(
        "stt", "whisper_local", "", "Whisper Local",
        config={"device": "cuda", "compute_type": "float16"},
    )
    assert resolve_stt_local_device("whisper_local") == {"device": "cuda", "compute_type": "float16"}


def test_resolve_stt_engine_config_defaults_when_no_entry():
    assert resolve_stt_engine_config("whisper_local") == {
        "default_model": "phowhisper-medium",
        "vad_filter": True,
        "beam_size": 1,
        "condition_on_previous_text": False,
        "initial_prompt": "",
    }
    assert resolve_stt_engine_config("whisper_mlx") == {
        "model_path": "models/stt/phowhisper-medium-mlx",
        "condition_on_previous_text": False,
        "initial_prompt": "",
    }
    assert resolve_stt_engine_config("qwen3_asr") == {"default_model": "Qwen/Qwen3-ASR-0.6B"}
    assert resolve_stt_engine_config("vosk") == {
        "model_path": "models/stt/vosk-model-small-en-us-0.15",
    }


@pytest.mark.asyncio
async def test_resolve_stt_engine_config_merges_registry_config_over_defaults():
    await model_registry_store.create(
        "stt", "whisper_local", "", "Whisper Local",
        config={"default_model": "phowhisper-large", "beam_size": 5, "device": "cuda"},
    )
    cfg = resolve_stt_engine_config("whisper_local")
    assert cfg["default_model"] == "phowhisper-large"
    assert cfg["beam_size"] == 5
    assert cfg["vad_filter"] is True  # untouched default
    assert cfg["device"] == "cuda"  # extra keys (device config) pass through


@pytest.mark.asyncio
async def test_resolve_stt_engine_config_ignores_seed_governance_row():
    """seed_known_models() creates ENABLED per-model-size governance rows under
    the same (kind="stt", engine) pair -- only the model_id="" sentinel row may
    feed engine config (same hazard as resolve_stt_local_device)."""
    await model_registry_store.create(
        "stt", "qwen3_asr", "1.7b", "Qwen3-ASR 1.7B", config={"default_model": "wrong"},
    )
    assert resolve_stt_engine_config("qwen3_asr") == {"default_model": "Qwen/Qwen3-ASR-0.6B"}


def test_resolve_omnivoice_config_defaults_when_no_entry():
    assert resolve_omnivoice_config() == OmnivoiceConfig()


@pytest.mark.asyncio
async def test_resolve_omnivoice_config_reads_registry_entry():
    await model_registry_store.create(
        "tts", "omnivoice", "", "OmniVoice",
        config={
            "omnivoice_model_id": "k2-fsa/OmniVoice-custom",
            "omnivoice_device": "mps",
            "omnivoice_dtype": "bfloat16",
        },
    )
    cfg = resolve_omnivoice_config()
    assert cfg.omnivoice_model_id == "k2-fsa/OmniVoice-custom"
    assert cfg.omnivoice_device == "mps"
    assert cfg.omnivoice_dtype == "bfloat16"
    assert cfg.omnivoice_server_host == "127.0.0.1"  # untouched default


@pytest.mark.asyncio
async def test_resolve_omnivoice_config_ignores_seed_governance_row():
    """Regression guard: seed_known_models() creates one ENABLED
    tts/omnivoice/omnivoice governance row (config={}) -- it must never
    shadow the real model_id="" config row. Before the fix,
    resolve_omnivoice_config() used find_enabled_sync("tts", "omnivoice"),
    which ignores model_id and would return whichever of these two rows the
    cache happened to iterate first."""
    await model_registry_store.create(
        "tts", "omnivoice", "omnivoice", "OmniVoice", config={},
    )
    await model_registry_store.create(
        "tts", "omnivoice", "", "OmniVoice",
        config={"omnivoice_model_id": "k2-fsa/OmniVoice-custom", "omnivoice_device": "mps"},
    )
    cfg = resolve_omnivoice_config()
    assert cfg.omnivoice_model_id == "k2-fsa/OmniVoice-custom"
    assert cfg.omnivoice_device == "mps"


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


@pytest.mark.asyncio
async def test_resolve_remote_stt_config_honors_explicit_zero_timeout():
    """Verify that an explicitly-configured timeout_seconds: 0.0 is honored,
    not silently treated as "not configured" and replaced by the default."""
    await model_registry_store.create(
        "stt", "whisper_service", "gpt-4o-transcribe", "Whisper Service",
        base_url="https://api.example.com/v1", api_key="sk-abc",
        config={"timeout_seconds": 0.0},
    )
    cfg = resolve_remote_stt_config()
    assert cfg.remote_stt_timeout_seconds == 0.0
