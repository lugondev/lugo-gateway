import pytest

from app.core.errors import AppError
from app.services.models import ModelManager, model_manager


def test_validate_rejects_unsafe_names():
    with pytest.raises(AppError):
        model_manager.validate("../etc/passwd")
    with pytest.raises(AppError):
        model_manager.validate("bad name")


def test_validate_accepts_model_names():
    model_manager.validate("vosk-model-small-en-us-0.15")  # no raise


def test_delete_missing_raises():
    with pytest.raises(AppError):
        model_manager.delete("vosk-model-not-installed-xyz")


def test_snapshot_shape():
    snap = model_manager.snapshot()
    assert set(snap) == {"installed", "suggestions", "active", "jobs", "base_dir"}
    assert isinstance(snap["installed"], list)
    assert all("installed" in s for s in snap["suggestions"])


def test_resolved_dir_blocks_traversal(tmp_path):
    mgr = ModelManager()
    with pytest.raises(AppError):
        mgr._resolved_dir("../escape")
