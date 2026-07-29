"""Local STT providers read engine-level settings (default model / model path,
whisper decode tuning) from the Model Registry model_id="" sentinel row via
resolve_stt_engine_config() -- not from SttLocalConfig, whose per-engine fields
were removed (models are all registry entries now, none get special System
settings)."""

import pytest

from app.services.model_registry.store import model_registry_store
from app.services.stt.providers import qwen3_asr_provider, vosk_provider, whisper_provider
from app.services.stt.providers.whisper_mlx_provider import WhisperMlxProvider


@pytest.mark.asyncio
async def test_get_active_whisper_model_reads_registry_sentinel(monkeypatch):
    monkeypatch.setattr(whisper_provider, "_active_model", None)
    await model_registry_store.create(
        "stt", "whisper_local", "", "Whisper Local (engine config)",
        config={"default_model": "large-v3"},
    )
    assert whisper_provider.get_active_whisper_model() == "large-v3"


def test_get_active_whisper_model_default_without_registry_row(monkeypatch):
    monkeypatch.setattr(whisper_provider, "_active_model", None)
    assert whisper_provider.get_active_whisper_model() == "large-v3-turbo"


@pytest.mark.asyncio
async def test_get_active_qwen3_asr_model_reads_registry_sentinel():
    qwen3_asr_provider.set_active_qwen3_asr_model(None)
    await model_registry_store.create(
        "stt", "qwen3_asr", "", "Qwen3-ASR (engine config)",
        config={"default_model": "Qwen/Qwen3-ASR-1.7B"},
    )
    try:
        assert qwen3_asr_provider.get_active_qwen3_asr_model() == "Qwen/Qwen3-ASR-1.7B"
    finally:
        qwen3_asr_provider.set_active_qwen3_asr_model(None)


@pytest.mark.asyncio
async def test_get_active_vosk_path_reads_registry_sentinel(monkeypatch):
    monkeypatch.setattr(vosk_provider, "_active_path", None)
    await model_registry_store.create(
        "stt", "vosk", "", "Vosk (engine config)",
        config={"model_path": "models/stt/vosk-vn"},
    )
    assert vosk_provider.get_active_vosk_path() == "models/stt/vosk-vn"


@pytest.mark.asyncio
async def test_whisper_mlx_detail_reads_registry_model_path():
    await model_registry_store.create(
        "stt", "whisper_mlx", "", "Whisper MLX (engine config)",
        config={"model_path": "models/stt/custom-mlx-dir"},
    )
    assert WhisperMlxProvider().detail() == "custom-mlx-dir · Apple GPU (MLX)"


@pytest.mark.asyncio
async def test_whisper_transcribe_uses_registry_decode_tuning(monkeypatch):
    captured = {}

    class FakeModel:
        def transcribe(self, path, **kw):
            captured.update(kw)
            return [], None

    await model_registry_store.create(
        "stt", "whisper_local", "", "Whisper Local (engine config)",
        config={
            "default_model": "large-v3",
            "vad_filter": False,
            "beam_size": 7,
            "condition_on_previous_text": True,
            "initial_prompt": "ngữ cảnh",
        },
    )
    monkeypatch.setattr(whisper_provider, "_active_model", None)
    provider = whisper_provider.WhisperProvider()
    key = provider._cache_key("large-v3")
    monkeypatch.setitem(whisper_provider._MODEL_CACHE, key, FakeModel())

    provider._do_transcribe(b"\x00\x00", "vi", None)

    assert captured["vad_filter"] is False
    assert captured["beam_size"] == 7
    assert captured["condition_on_previous_text"] is True
    assert captured["initial_prompt"] == "ngữ cảnh"
