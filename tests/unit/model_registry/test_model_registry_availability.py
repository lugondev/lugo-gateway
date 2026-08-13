"""is_artifact_installed: whether a specific (kind, engine, model_id) registry
entry corresponds to an artifact actually present on disk, for the local
engines with a real download/delete lifecycle through the Models page
(whisper, vosk, omnivoice, vieneu). Everything else (service/remote engines,
model_id="" sentinel rows, package-only engines) is "not applicable" -> None,
so the enable-guard never blocks those."""

import pytest

from app.services.model_registry.availability import is_artifact_installed


def test_whisper_cached_size_is_installed(monkeypatch):
    from app.services.whisper_models import whisper_manager

    monkeypatch.setattr(
        whisper_manager, "snapshot",
        lambda: {"models": [{"size": "medium", "label": "Medium", "cached": True}]},
    )
    assert is_artifact_installed("stt", "whisper", "medium") is True


def test_whisper_local_alias_uses_same_snapshot(monkeypatch):
    from app.services.whisper_models import whisper_manager

    monkeypatch.setattr(
        whisper_manager, "snapshot",
        lambda: {"models": [{"size": "medium", "label": "Medium", "cached": True}]},
    )
    assert is_artifact_installed("stt", "whisper_local", "medium") is True


def test_whisper_uncached_size_is_not_installed(monkeypatch):
    from app.services.whisper_models import whisper_manager

    monkeypatch.setattr(
        whisper_manager, "snapshot",
        lambda: {"models": [{"size": "medium", "label": "Medium", "cached": False}]},
    )
    assert is_artifact_installed("stt", "whisper", "medium") is False


def test_whisper_unknown_size_is_not_installed(monkeypatch):
    from app.services.whisper_models import whisper_manager

    monkeypatch.setattr(
        whisper_manager, "snapshot",
        lambda: {"models": [{"size": "medium", "label": "Medium", "cached": True}]},
    )
    assert is_artifact_installed("stt", "whisper", "large-v3") is False


def test_vosk_installed_name_is_installed(monkeypatch):
    from app.services.models import model_manager

    monkeypatch.setattr(
        model_manager, "snapshot",
        lambda: {"installed": [{"name": "vosk-model-small-vn-0.4", "active": True}]},
    )
    assert is_artifact_installed("stt", "vosk", "vosk-model-small-vn-0.4") is True


def test_vosk_not_installed_name_is_not_installed(monkeypatch):
    from app.services.models import model_manager

    monkeypatch.setattr(
        model_manager, "snapshot",
        lambda: {"installed": [{"name": "vosk-model-small-vn-0.4", "active": True}]},
    )
    assert is_artifact_installed("stt", "vosk", "vosk-model-vn-0.4") is False


def test_omnivoice_cached_repo_is_installed(monkeypatch):

    monkeypatch.setattr("app.core.hf_cache.repo_cached", lambda repo: repo == "k2-fsa/OmniVoice")
    assert is_artifact_installed("tts", "omnivoice", "k2-fsa/OmniVoice") is True
    assert is_artifact_installed("tts", "omnivoice", "some/other-repo") is False


def test_vieneu_known_mode_maps_through_vieneu_modes(monkeypatch):
    monkeypatch.setattr("app.core.hf_cache.repo_cached", lambda repo: repo == "pnnbao-ump/VieNeu-TTS-v3-Turbo")
    assert is_artifact_installed("tts", "vieneu", "v3turbo") is True

    monkeypatch.setattr("app.core.hf_cache.repo_cached", lambda repo: False)
    assert is_artifact_installed("tts", "vieneu", "v3turbo") is False


def test_vieneu_unknown_mode_is_not_installed():
    assert is_artifact_installed("tts", "vieneu", "totally-unknown-mode") is False


def test_vieneu_mode_with_no_repo_is_not_installed():
    # "standard"/"turbo"/"fast" map to repo=None (needs vieneu[gpu], no HF cache concept)
    assert is_artifact_installed("tts", "vieneu", "standard") is False


@pytest.mark.parametrize("kind,engine", [
    ("stt", "edge_tts"),
    ("stt", "qwen3_asr"),
    ("stt", "http_stt"),
    ("tts", "edge_tts"),
    ("tts", "qwen3_tts_0_6b"),
    ("llm", "openrouter"),
])
def test_non_special_cased_engine_is_not_applicable(kind, engine):
    assert is_artifact_installed(kind, engine, "some-model-id") is None


@pytest.mark.parametrize("kind,engine", [
    ("stt", "whisper"),
    ("stt", "vosk"),
    ("tts", "omnivoice"),
    ("tts", "vieneu"),
    ("stt", "edge_tts"),
    ("llm", "openrouter"),
])
def test_empty_model_id_sentinel_row_is_not_applicable(kind, engine):
    assert is_artifact_installed(kind, engine, "") is None


@pytest.mark.parametrize("kind,engine", [
    ("tts", "omnivoice"),
    ("tts", "vieneu"),
])
def test_engine_equals_model_id_shim_row_is_not_applicable(kind, engine):
    """The (engine, engine) shim these entries use to satisfy the TTS-profile
    gate (see seed_installed_models_to_registry's TTS backfill) is NOT a real
    HF repo id / vieneu mode -- checking it against repo_cached()/VIENEU_MODES
    would always report "not installed" for a profile that's actually working
    fine, since model_id == engine never matches any real artifact identifier."""
    assert is_artifact_installed(kind, engine, engine) is None
