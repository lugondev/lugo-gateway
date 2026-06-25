import pytest

from app.core.errors import AppError
from app.services.tts.providers.omnivoice_provider import get_active_omnivoice_model
from app.services.tts.providers.vieneu_provider import get_active_vieneu_mode
from app.services.tts_models import tts_model_manager


def test_snapshot_shape():
    snap = tts_model_manager.snapshot()
    assert set(snap) == {"omnivoice", "vieneu"}
    assert {"active", "models"} <= set(snap["omnivoice"])
    assert {"active", "modes"} <= set(snap["vieneu"])
    assert all({"id", "cached", "active"} <= set(m) for m in snap["omnivoice"]["models"])
    assert all({"mode", "cpu", "cached", "active"} <= set(m) for m in snap["vieneu"]["modes"])


def test_validate_repo_rejects_bad_ids():
    with pytest.raises(AppError):
        tts_model_manager.validate_repo("no-slash")
    with pytest.raises(AppError):
        tts_model_manager.validate_repo("bad id/x")


def test_validate_mode():
    tts_model_manager.validate_mode("v3turbo")  # ok
    with pytest.raises(AppError):
        tts_model_manager.validate_mode("../x")


def test_select_omnivoice_then_restore():
    original = get_active_omnivoice_model()
    try:
        tts_model_manager.select_omnivoice("some-org/some-model")
        assert get_active_omnivoice_model() == "some-org/some-model"
    finally:
        tts_model_manager.select_omnivoice(original)


def test_select_vieneu_then_restore():
    original = get_active_vieneu_mode()
    try:
        tts_model_manager.select_vieneu("turbo")
        assert get_active_vieneu_mode() == "turbo"
    finally:
        tts_model_manager.select_vieneu(original)


def test_delete_uncached_raises():
    with pytest.raises(AppError):
        tts_model_manager.delete_omnivoice("nonexistent-org/nonexistent-model")
