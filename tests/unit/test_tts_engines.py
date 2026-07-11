import pytest

from app.core.errors import EngineNotFoundError, ProviderError
from app.schemas.tts import TTSRequest
from app.services.tts.base import RenderingTTSProvider
from app.services.tts.providers.edge_tts_provider import EdgeTTSProvider
from app.services.tts.service import tts_service


def test_lists_omnivoice_and_vieneu():
    engines = {e["engine"] for e in tts_service.list_engines()}
    assert {"omnivoice", "vieneu"} <= engines


def test_engine_entries_have_expected_keys():
    for e in tts_service.list_engines():
        assert {"engine", "available", "detail", "default"} <= set(e)


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


def test_lists_kokoro_vi():
    engines = {e["engine"] for e in tts_service.list_engines()}
    assert "kokoro_vi" in engines


def test_kokoro_vi_voices_shape():
    voices = tts_service.get_provider("kokoro_vi").list_voices()
    # kokoro-vietnamese is installed in this env -> returns preset voicepacks
    assert isinstance(voices, list)
    if voices:
        assert {"label", "voice"} <= set(voices[0])


async def test_render_failure_raises_provider_error_no_silent_fallback():
    class _BrokenTTS(RenderingTTSProvider):
        name = "broken-tts"

        async def _render_wav(self, payload: TTSRequest) -> bytes:
            raise RuntimeError("model not loaded")

    with pytest.raises(ProviderError):
        await _BrokenTTS().synthesize(TTSRequest(text="hi"))


def test_lists_edge_tts():
    engines = {e["engine"] for e in tts_service.list_engines()}
    assert "edge_tts" in engines


def test_edge_tts_voices_shape():
    voices = tts_service.get_provider("edge_tts").list_voices()
    assert voices == [
        {"label": "Hoài My (nữ)", "voice": "vi-VN-HoaiMyNeural"},
        {"label": "Nam Minh (nam)", "voice": "vi-VN-NamMinhNeural"},
    ]


def test_edge_tts_rate_str():
    assert EdgeTTSProvider._rate_str(None) == "+0%"
    assert EdgeTTSProvider._rate_str(1.0) == "+0%"
    assert EdgeTTSProvider._rate_str(1.2) == "+20%"
    assert EdgeTTSProvider._rate_str(0.8) == "-20%"


def test_edge_tts_estimate_duration():
    from app.services.tts.providers.edge_tts_provider import _estimate_duration_seconds

    # 48000 bits/s CBR -> 6000 bytes/s
    assert _estimate_duration_seconds(b"x" * 6000) == pytest.approx(1.0)
    assert _estimate_duration_seconds(b"") == 0.0
