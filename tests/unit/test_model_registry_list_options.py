import pytest
from app.services.model_registry.store import model_registry_store


async def test_list_options_filters_enabled_stable_and_skips_sentinel():
    await model_registry_store.create("stt", "whisper", "tiny", "Tiny", enabled=True)
    await model_registry_store.create("stt", "whisper", "large-v3", "Large", enabled=True)
    await model_registry_store.create("stt", "whisper", "", "Whisper config", enabled=True)  # sentinel
    await model_registry_store.create("stt", "vosk", "vn", "Vosk VN", enabled=False)  # disabled
    await model_registry_store.create("stt", "qwen3_asr", "1.7b", "Q Testing", enabled=True, stage="testing")

    stable = await model_registry_store.list_options("stt", can_use_testing=False)
    assert stable == [
        {"engine": "whisper", "model_id": "large-v3", "label": "Large"},
        {"engine": "whisper", "model_id": "tiny", "label": "Tiny"},
    ]

    with_testing = await model_registry_store.list_options("stt", can_use_testing=True)
    assert {"engine": "qwen3_asr", "model_id": "1.7b", "label": "Q Testing"} in with_testing
    assert len(with_testing) == 3
