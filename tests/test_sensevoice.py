"""SenseVoice STT engine: registered, auto-hidden without funasr, in the catalog."""

import app.services.stt.providers.sensevoice_provider as sv_mod
from app.services.recommend.catalog import CANDIDATES
from app.services.stt.providers.sensevoice_provider import SenseVoiceProvider
from app.services.stt.service import stt_service


def test_sensevoice_registered():
    assert "sensevoice" in stt_service.providers
    assert isinstance(stt_service.providers["sensevoice"], SenseVoiceProvider)


def test_available_tracks_sherpa_onnx(monkeypatch):
    p = stt_service.providers["sensevoice"]
    monkeypatch.setattr(sv_mod, "module_available", lambda m: m == "sherpa_onnx")
    assert p.available() is True
    monkeypatch.setattr(sv_mod, "module_available", lambda m: False)
    assert p.available() is False


def test_listed_in_engines_unavailable_in_dev():
    # sherpa-onnx is not a dev/test dependency, so the engine must report unavailable
    # (auto-hidden) rather than erroring.
    engines = {e["engine"]: e for e in stt_service.list_engines()}
    assert "sensevoice" in engines
    assert engines["sensevoice"]["mode"] == "local"
    assert engines["sensevoice"]["available"] is False


def test_stt_request_schema_accepts_sensevoice():
    # The /transcribe route validates engine against STTRequest's pattern — it must
    # include sensevoice, or the engine is unusable via the API (regression).
    from app.schemas.stt import STTRequest

    assert STTRequest(engine="sensevoice").engine == "sensevoice"


def test_in_recommend_catalog():
    sv = [c for c in CANDIDATES if c.engine == "sensevoice"]
    assert sv, "expected a SenseVoice candidate"
    c = sv[0]
    assert c.category == "stt"
    assert c.chip == "cpu"
    assert "sherpa_onnx" in c.requires
    assert c.vietnamese is False  # SenseVoice has no Vietnamese support
