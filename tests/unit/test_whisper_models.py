import pytest

from app.core.errors import AppError
from app.services.stt.providers.whisper_provider import get_active_whisper_model
from app.services.whisper_models import whisper_manager


def test_validate_rejects_unsafe_sizes():
    with pytest.raises(AppError):
        whisper_manager.validate("../escape")
    with pytest.raises(AppError):
        whisper_manager.validate("bad size")


def test_validate_accepts_sizes():
    whisper_manager.validate("large-v3")  # no raise


def test_snapshot_shape():
    snap = whisper_manager.snapshot()
    assert set(snap) == {"models", "active"}
    sizes = {m["size"] for m in snap["models"]}
    assert {"tiny", "small", "large-v3"} <= sizes
    assert all({"cached", "active", "size_bytes"} <= set(m) for m in snap["models"])


def test_select_changes_active_then_restore():
    original = get_active_whisper_model()
    try:
        whisper_manager.select("tiny")
        assert get_active_whisper_model() == "tiny"
        assert whisper_manager.snapshot()["active"] == "tiny"
    finally:
        whisper_manager.select(original)


def test_delete_uncached_raises():
    with pytest.raises(AppError):
        whisper_manager.delete("definitely-not-cached-size")
