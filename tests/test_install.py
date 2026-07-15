"""Runtime pip-install endpoint: gated by ALLOW_RUNTIME_INSTALL, allowlist-only."""

import pytest
from fastapi.testclient import TestClient

from app.core.errors import AppError
from app.core.settings import settings
from app.main import app
from app.services.install_manager import install_manager

client = TestClient(app)


def test_validate_raises_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "allow_runtime_install", False)
    with pytest.raises(AppError):
        install_manager.validate("vieneu")


def test_validate_rejects_non_allowlist(monkeypatch):
    monkeypatch.setattr(settings, "allow_runtime_install", True)
    with pytest.raises(AppError):
        install_manager.validate("evil-package; rm -rf /")


def test_validate_allows_known_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "allow_runtime_install", True)
    for pkg in (
        "vieneu", "qwen_asr", "mlx_qwen3_asr", "silero_vad", "pyannote.audio",
        "qwen_tts", "voxcpm", "edge_tts",
    ):
        install_manager.validate(pkg)  # must not raise


def test_endpoint_403_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "allow_runtime_install", False)
    resp = client.post("/v1/models/install", json={"package": "vieneu"})
    assert resp.status_code == 403


def test_recommend_reflects_install_enabled(monkeypatch):
    from app.services.recommend.service import recommend_all

    monkeypatch.setattr(settings, "allow_runtime_install", False)
    assert recommend_all()["install_enabled"] is False
    monkeypatch.setattr(settings, "allow_runtime_install", True)
    assert recommend_all()["install_enabled"] is True
