"""Qwen3-ASR MLX STT engine: registered, Apple-only (auto-hidden), in catalog."""

import app.services.stt.providers.qwen3_asr_provider as q_mod
from app.services.recommend.catalog import CANDIDATES
from app.services.stt.providers.qwen3_asr_provider import Qwen3AsrProvider
from app.services.stt.service import stt_service


def test_registered():
    assert "qwen3_asr" in stt_service.providers
    assert isinstance(stt_service.providers["qwen3_asr"], Qwen3AsrProvider)


def test_available_tracks_mlx_package(monkeypatch):
    p = stt_service.providers["qwen3_asr"]
    monkeypatch.setattr(q_mod, "module_available", lambda m: m == "mlx_qwen3_asr")
    assert p.available() is True
    monkeypatch.setattr(q_mod, "module_available", lambda m: False)
    assert p.available() is False


def test_listed_reflects_package_presence():
    from app.core.deps import module_available

    engines = {e["engine"]: e for e in stt_service.list_engines()}
    assert "qwen3_asr" in engines
    assert engines["qwen3_asr"]["mode"] == "local"
    assert engines["qwen3_asr"]["available"] == module_available("mlx_qwen3_asr")


def test_stt_request_schema_accepts_qwen3_asr():
    from app.schemas.stt import STTRequest

    assert STTRequest(engine="qwen3_asr").engine == "qwen3_asr"


def test_in_recommend_catalog_apple_vietnamese():
    c = [x for x in CANDIDATES if x.engine == "qwen3_asr"]
    assert c, "expected a Qwen3-ASR candidate"
    c = c[0]
    assert c.category == "stt"
    assert c.chip == "apple_silicon"
    assert c.vietnamese is True  # Qwen3-ASR supports Vietnamese (verified)
