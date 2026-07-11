import pytest

from app.core.errors import AppError
from app.services.stt.model_registry import (
    STT_MODEL_REGISTRIES,
    apply_stt_model,
    qwen3_asr_model_registry,
    resolve_default_stt_model,
)
from app.services.stt.providers import qwen3_asr_provider as q
from app.services.stt.providers.whisper_provider import get_active_whisper_model
from app.services.whisper_models import whisper_manager


@pytest.fixture(autouse=True)
def _reset_qwen3():
    q.set_active_qwen3_asr_model(None)
    yield
    q.set_active_qwen3_asr_model(None)


def test_whisper_manager_list_models_shape():
    models = whisper_manager.list_models()
    assert models  # non-empty
    assert all({"id", "label", "cached", "active"} <= set(m) for m in models)
    ids = {m["id"] for m in models}
    assert {"tiny", "phowhisper-medium", "large-v3"} <= ids


def test_qwen3_registry_list_models_shape():
    models = qwen3_asr_model_registry.list_models()
    ids = {m["id"] for m in models}
    assert ids == {"0.6b", "1.7b"}
    assert all({"id", "label", "cached", "active"} <= set(m) for m in models)


def test_qwen3_registry_validate_rejects_unknown():
    with pytest.raises(AppError):
        qwen3_asr_model_registry.validate("7b-does-not-exist")


def test_qwen3_registry_validate_accepts_known():
    qwen3_asr_model_registry.validate("0.6b")  # no raise
    qwen3_asr_model_registry.validate("1.7B")  # case-insensitive, no raise


def test_qwen3_registry_select_changes_active():
    qwen3_asr_model_registry.select("1.7b")
    assert q.get_active_qwen3_asr_model() == "Qwen/Qwen3-ASR-1.7B"
    models = qwen3_asr_model_registry.list_models()
    active = {m["id"]: m["active"] for m in models}
    assert active == {"0.6b": False, "1.7b": True}


def test_registries_dict_covers_whisper_family_and_qwen3():
    assert STT_MODEL_REGISTRIES["whisper"] is whisper_manager
    assert STT_MODEL_REGISTRIES["whisper_local"] is whisper_manager
    assert STT_MODEL_REGISTRIES["whisper_gemma"] is whisper_manager
    assert STT_MODEL_REGISTRIES["qwen3_asr"] is qwen3_asr_model_registry
    assert "vosk" not in STT_MODEL_REGISTRIES
    assert "whisper_mlx" not in STT_MODEL_REGISTRIES


def test_apply_stt_model_noop_for_empty_model():
    before = q.get_active_qwen3_asr_model()
    apply_stt_model("qwen3_asr", "")  # must not raise
    assert q.get_active_qwen3_asr_model() == before


def test_apply_stt_model_noop_for_engine_without_registry():
    apply_stt_model("vosk", "anything")  # must not raise, no registry to apply to


def test_apply_stt_model_selects_for_known_engine():
    apply_stt_model("qwen3_asr", "1.7b")
    assert q.get_active_qwen3_asr_model() == "Qwen/Qwen3-ASR-1.7B"


def test_apply_stt_model_raises_for_invalid_model_on_known_engine():
    with pytest.raises(AppError):
        apply_stt_model("qwen3_asr", "not-a-real-size")


def test_resolve_default_stt_model_whisper_family_matches_active_global():
    for engine in ("whisper", "whisper_local", "whisper_gemma"):
        assert resolve_default_stt_model(engine) == get_active_whisper_model()


def test_resolve_default_stt_model_qwen3_asr_matches_active_global():
    assert resolve_default_stt_model("qwen3_asr") == q.get_active_qwen3_asr_model()


def test_resolve_default_stt_model_returns_none_for_engine_without_registry():
    assert resolve_default_stt_model("vosk") is None
