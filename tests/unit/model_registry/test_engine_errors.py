import pytest

from app.core.errors import EngineNotFoundError
from app.services.stt.service import stt_service
from app.services.tts.service import tts_service


def test_unknown_stt_engine_raises_domain_error():
    with pytest.raises(EngineNotFoundError):
        stt_service.get_provider("does-not-exist")


def test_unknown_tts_engine_raises_domain_error():
    with pytest.raises(EngineNotFoundError):
        tts_service.get_provider("does-not-exist")


def test_known_engines_resolve():
    assert stt_service.get_provider("vosk").name == "vosk"
    assert stt_service.get_provider("whisper").name == "whisper_local"
    assert tts_service.get_provider("omnivoice").name == "omnivoice"
