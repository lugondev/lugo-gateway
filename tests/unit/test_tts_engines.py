import sys
import types

import numpy as np
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
        assert {"engine", "available", "detail", "default", "install_package", "install_enabled"} <= set(e)


def test_engine_entries_expose_install_package_for_pip_installable_engines(monkeypatch):
    from app.core.settings import settings

    monkeypatch.setattr(settings, "allow_runtime_install", True)
    entries = {e["engine"]: e for e in tts_service.list_engines()}
    assert entries["vieneu"]["install_package"] == "vieneu"
    assert entries["edge_tts"]["install_package"] == "edge_tts"
    assert entries["qwen3_tts_0_6b"]["install_package"] == "qwen_tts"
    assert entries["qwen3_tts_1_7b"]["install_package"] == "qwen_tts"
    assert entries["voxcpm2"]["install_package"] == "voxcpm"
    assert all(e["install_enabled"] is True for e in entries.values())
    # kokoro_vi installs from a git URL, not a plain pip spec -- no one-click install.
    assert entries["kokoro_vi"]["install_package"] is None
    # omnivoice is gated by a config path, not a pip package.
    assert entries["omnivoice"]["install_package"] is None


def test_get_provider_resolves_and_rejects():
    assert tts_service.get_provider("vieneu").name == "vieneu"
    assert tts_service.get_provider("omnivoice").name == "omnivoice"
    with pytest.raises(EngineNotFoundError):
        tts_service.get_provider("nope")


async def test_vieneu_voices_shape():
    voices = await tts_service.get_provider("vieneu").list_voices()
    # vieneu is installed in this env -> returns preset voices
    assert isinstance(voices, list)
    if voices:
        assert {"label", "voice"} <= set(voices[0])


async def test_vieneu_supports_voice_clone():
    assert await tts_service.get_provider("vieneu").supports_voice_clone() is True


def test_lists_kokoro_vi():
    engines = {e["engine"] for e in tts_service.list_engines()}
    assert "kokoro_vi" in engines


async def test_kokoro_vi_voices_shape():
    voices = await tts_service.get_provider("kokoro_vi").list_voices()
    # kokoro-vietnamese is installed in this env -> returns preset voicepacks
    assert isinstance(voices, list)
    if voices:
        assert {"label", "voice"} <= set(voices[0])


async def test_kokoro_vi_does_not_support_voice_clone():
    # fixed voicepacks only, per the module docstring -- no ref-audio cloning.
    assert await tts_service.get_provider("kokoro_vi").supports_voice_clone() is False


async def test_voxcpm2_supports_voice_clone():
    assert await tts_service.get_provider("voxcpm2").supports_voice_clone() is True


async def test_omnivoice_supports_voice_clone():
    assert await tts_service.get_provider("omnivoice").supports_voice_clone() is True


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


async def test_edge_tts_voices_shape():
    voices = await tts_service.get_provider("edge_tts").list_voices()
    assert voices == [
        {"label": "Hoài My (nữ)", "voice": "vi-VN-HoaiMyNeural"},
        {"label": "Nam Minh (nam)", "voice": "vi-VN-NamMinhNeural"},
    ]


async def test_edge_tts_does_not_support_voice_clone():
    assert await tts_service.get_provider("edge_tts").supports_voice_clone() is False


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


def _install_fake_edge_tts(monkeypatch, communicate_cls):
    """edge_tts is an optional dependency not installed in this test env, so
    `import edge_tts` inside synthesize() needs a stub module injected into
    sys.modules (mirrors tests/test_qwen3_asr.py's mlx_qwen3_asr stubbing)."""
    fake_mod = types.ModuleType("edge_tts")
    fake_mod.Communicate = communicate_cls
    monkeypatch.setitem(sys.modules, "edge_tts", fake_mod)


async def test_edge_tts_synthesize_wraps_stream_exception_as_provider_error(monkeypatch):
    class _BrokenCommunicate:
        def __init__(self, *args, **kwargs):
            pass

        async def stream(self):
            raise RuntimeError("network unreachable")
            yield {}  # pragma: no cover - makes this an async generator

    _install_fake_edge_tts(monkeypatch, _BrokenCommunicate)

    with pytest.raises(ProviderError):
        await EdgeTTSProvider().synthesize(TTSRequest(text="hi"))


async def test_edge_tts_synthesize_raises_on_no_audio_received(monkeypatch):
    class _SilentCommunicate:
        def __init__(self, *args, **kwargs):
            pass

        async def stream(self):
            # No audio chunks at all -- only a non-audio (e.g. word-boundary) event.
            yield {"type": "WordBoundary"}
            return

    _install_fake_edge_tts(monkeypatch, _SilentCommunicate)

    with pytest.raises(ProviderError, match="no audio received"):
        await EdgeTTSProvider().synthesize(TTSRequest(text="hi"))


async def test_edge_tts_synthesize_retries_and_succeeds_after_transient_failure(monkeypatch):
    from app.services.tts.providers import edge_tts_provider

    monkeypatch.setattr(edge_tts_provider, "_RETRY_DELAY_SECONDS", 0)
    attempts = []

    class _FlakyCommunicate:
        def __init__(self, *args, **kwargs):
            attempts.append(1)

        async def stream(self):
            if len(attempts) == 1:
                raise RuntimeError("connection reset")
            yield {"type": "audio", "data": b"x" * 100}

    _install_fake_edge_tts(monkeypatch, _FlakyCommunicate)

    result = await EdgeTTSProvider().synthesize(TTSRequest(text="hi"))

    assert len(attempts) == 2
    assert result.engine == "edge_tts"


async def test_edge_tts_synthesize_gives_up_after_max_attempts(monkeypatch):
    from app.services.tts.providers import edge_tts_provider

    monkeypatch.setattr(edge_tts_provider, "_RETRY_DELAY_SECONDS", 0)
    attempts = []

    class _AlwaysBrokenCommunicate:
        def __init__(self, *args, **kwargs):
            attempts.append(1)

        async def stream(self):
            raise RuntimeError("connection reset")
            yield {}  # pragma: no cover - makes this an async generator

    _install_fake_edge_tts(monkeypatch, _AlwaysBrokenCommunicate)

    with pytest.raises(ProviderError, match="connection reset"):
        await EdgeTTSProvider().synthesize(TTSRequest(text="hi"))

    assert len(attempts) == edge_tts_provider._MAX_ATTEMPTS


def test_pick_device_dtype_attn_prefers_cuda(monkeypatch):
    import torch

    from app.services.tts.providers.qwen3_tts_provider import _pick_device_dtype_attn

    monkeypatch.delenv("QWEN3_TTS_DEVICE", raising=False)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)

    device, dtype, attn = _pick_device_dtype_attn()

    assert device == "cuda:0"
    assert dtype is torch.bfloat16
    assert attn == "flash_attention_2"


def test_pick_device_dtype_attn_falls_back_to_mps(monkeypatch):
    """MPS must use float32, not float16.

    Empirically reproduced (3/3 runs, three different input texts): fp16 on
    MPS makes Qwen3-TTS's nested codec `generate` produce inf/nan in its
    sampling distribution, and `torch.multinomial` raises
    "probability tensor contains either `inf`, `nan` or element < 0".
    float32 on MPS works and still uses the Apple GPU (no CPU fallback).
    """
    import torch

    from app.services.tts.providers.qwen3_tts_provider import _pick_device_dtype_attn

    monkeypatch.delenv("QWEN3_TTS_DEVICE", raising=False)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: True)

    device, dtype, attn = _pick_device_dtype_attn()

    assert device == "mps"
    assert dtype is torch.float32
    assert attn is None


def test_pick_device_dtype_attn_falls_back_to_cpu(monkeypatch):
    import torch

    from app.services.tts.providers.qwen3_tts_provider import _pick_device_dtype_attn

    monkeypatch.delenv("QWEN3_TTS_DEVICE", raising=False)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: False)

    device, dtype, attn = _pick_device_dtype_attn()

    assert device == "cpu"
    assert dtype is torch.float32
    assert attn is None


def test_pick_device_dtype_attn_honors_env_override(monkeypatch):
    import torch

    from app.services.tts.providers.qwen3_tts_provider import _pick_device_dtype_attn

    monkeypatch.setenv("QWEN3_TTS_DEVICE", "cpu")
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)  # must be ignored

    device, dtype, attn = _pick_device_dtype_attn()

    assert device == "cpu"
    assert dtype is torch.float32
    assert attn is None


def _install_fake_qwen_tts(monkeypatch, model_cls):
    """qwen_tts is an optional dependency not installed in this test env, so
    `from qwen_tts import Qwen3TTSModel` inside _load_model() needs a stub
    module injected into sys.modules (mirrors _install_fake_edge_tts above)."""
    fake_mod = types.ModuleType("qwen_tts")
    fake_mod.Qwen3TTSModel = model_cls
    monkeypatch.setitem(sys.modules, "qwen_tts", fake_mod)


def test_lists_qwen3_tts_engines():
    engines = {e["engine"] for e in tts_service.list_engines()}
    assert {"qwen3_tts_0_6b", "qwen3_tts_1_7b"} <= engines


def test_qwen3_tts_install_hint_mentions_package():
    provider = tts_service.get_provider("qwen3_tts_0_6b")
    assert "qwen-tts" in provider.install_hint()


async def test_qwen3_tts_voices_are_preset_speakers():
    from app.services.tts.providers.qwen3_tts_provider import PRESET_SPEAKERS

    voices = await tts_service.get_provider("qwen3_tts_1_7b").list_voices()
    assert voices == PRESET_SPEAKERS
    assert len(voices) == 9
    assert {"label", "voice"} <= set(voices[0])


async def test_qwen3_tts_supports_voice_clone():
    assert await tts_service.get_provider("qwen3_tts_0_6b").supports_voice_clone() is True


async def test_qwen3_tts_custom_voice_path_used_when_no_ref_audio(monkeypatch):
    from app.services.tts.providers import qwen3_tts_provider

    qwen3_tts_provider._CACHE.clear()
    calls = {}

    class _FakeModel:
        def generate_custom_voice(self, text, language, speaker, instruct):
            calls["custom_voice"] = (text, language, speaker, instruct)
            return np.array([0.0, 0.1, -0.1], dtype=np.float32), 24000

    class _FakeQwen3TTSModel:
        @staticmethod
        def from_pretrained(checkpoint_id, **kwargs):
            calls["checkpoint_id"] = checkpoint_id
            return _FakeModel()

    _install_fake_qwen_tts(monkeypatch, _FakeQwen3TTSModel)

    result = await tts_service.get_provider("qwen3_tts_0_6b").synthesize(TTSRequest(text="xin chào"))

    assert result.engine == "qwen3_tts_0_6b"
    assert result.sample_rate == 24000
    assert calls["checkpoint_id"] == "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
    assert calls["custom_voice"] == ("xin chào", "Auto", "Vivian", None)


async def test_qwen3_tts_voice_clone_path_used_when_ref_audio_present(monkeypatch):
    from app.services.tts.providers import qwen3_tts_provider

    qwen3_tts_provider._CACHE.clear()
    calls = {}

    class _FakeModel:
        def generate_voice_clone(self, text, language, ref_audio, ref_text, x_vector_only_mode):
            calls["voice_clone"] = (text, language, ref_audio, ref_text, x_vector_only_mode)
            return np.array([0.2, -0.2], dtype=np.float32), 24000

    class _FakeQwen3TTSModel:
        @staticmethod
        def from_pretrained(checkpoint_id, **kwargs):
            calls["checkpoint_id"] = checkpoint_id
            return _FakeModel()

    _install_fake_qwen_tts(monkeypatch, _FakeQwen3TTSModel)

    payload = TTSRequest(text="hi", ref_audio_path="/tmp/ref.wav", ref_text="reference text")
    result = await tts_service.get_provider("qwen3_tts_1_7b").synthesize(payload)

    assert result.sample_rate == 24000
    assert calls["checkpoint_id"] == "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
    assert calls["voice_clone"] == ("hi", "Auto", "/tmp/ref.wav", "reference text", False)


async def test_qwen3_tts_custom_voice_honors_explicit_voice_and_instruct(monkeypatch):
    from app.services.tts.providers import qwen3_tts_provider

    qwen3_tts_provider._CACHE.clear()
    calls = {}

    class _FakeModel:
        def generate_custom_voice(self, text, language, speaker, instruct):
            calls["custom_voice"] = (text, language, speaker, instruct)
            return np.array([0.0], dtype=np.float32), 24000

    class _FakeQwen3TTSModel:
        @staticmethod
        def from_pretrained(checkpoint_id, **kwargs):
            return _FakeModel()

    _install_fake_qwen_tts(monkeypatch, _FakeQwen3TTSModel)

    payload = TTSRequest(text="hello", voice="Ryan", instruct="cheerful", language="English")
    await tts_service.get_provider("qwen3_tts_0_6b").synthesize(payload)

    assert calls["custom_voice"] == ("hello", "English", "Ryan", "cheerful")
