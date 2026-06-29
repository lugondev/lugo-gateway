"""/v1/models exposes per-engine install status so the Models tab can tell the truth
(selected vs engine-installed vs weights-downloaded)."""

from fastapi.testclient import TestClient

from app.main import app
from app.services.tts.providers.vieneu_provider import VieNeuProvider

client = TestClient(app)


def test_vieneu_has_pip_install_hint():
    assert "pip install vieneu" in VieNeuProvider().install_hint()


def test_models_exposes_tts_engine_status_and_install_flag():
    data = client.get("/v1/models").json()["data"]
    assert "install_enabled" in data
    te = data["tts_engines"]
    assert "vieneu" in te and "omnivoice" in te
    assert "available" in te["vieneu"]
    assert "install_hint" in te["vieneu"]
