import pytest

from app.services.stt.providers import qwen3_asr_provider as q


@pytest.fixture(autouse=True)
def _reset_active():
    q.set_active_qwen3_asr_model(None)
    yield
    q.set_active_qwen3_asr_model(None)


def test_resolve_shorthand_to_full_repo():
    assert q.resolve_qwen3_asr_model("0.6b") == "Qwen/Qwen3-ASR-0.6B"
    assert q.resolve_qwen3_asr_model("1.7B") == "Qwen/Qwen3-ASR-1.7B"


def test_resolve_full_repo_passthrough():
    assert q.resolve_qwen3_asr_model("Qwen/Qwen3-ASR-1.7B") == "Qwen/Qwen3-ASR-1.7B"


def test_get_active_defaults_to_settings():
    from app.core.settings import settings

    assert q.get_active_qwen3_asr_model() == settings.qwen3_asr_model


def test_set_active_resolves_shorthand_and_roundtrips():
    q.set_active_qwen3_asr_model("1.7b")
    assert q.get_active_qwen3_asr_model() == "Qwen/Qwen3-ASR-1.7B"


def test_set_active_none_resets_to_settings():
    from app.core.settings import settings

    q.set_active_qwen3_asr_model("1.7b")
    q.set_active_qwen3_asr_model(None)
    assert q.get_active_qwen3_asr_model() == settings.qwen3_asr_model
