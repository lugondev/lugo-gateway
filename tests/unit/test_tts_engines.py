import pytest

from app.core.errors import EngineNotFoundError
from app.services.tts.service import tts_service


def test_lists_omnivoice_and_vieneu():
    engines = {e["engine"] for e in tts_service.list_engines()}
    assert {"omnivoice", "vieneu"} <= engines


def test_engine_entries_have_expected_keys():
    for e in tts_service.list_engines():
        assert {"engine", "available", "detail", "mock", "default"} <= set(e)


def test_get_provider_resolves_and_rejects():
    assert tts_service.get_provider("vieneu").name == "vieneu"
    assert tts_service.get_provider("omnivoice").name == "omnivoice"
    with pytest.raises(EngineNotFoundError):
        tts_service.get_provider("nope")


def test_vieneu_voices_shape():
    voices = tts_service.get_provider("vieneu").list_voices()
    # vieneu is installed in this env -> returns preset voices
    assert isinstance(voices, list)
    if voices:
        assert {"label", "voice"} <= set(voices[0])
