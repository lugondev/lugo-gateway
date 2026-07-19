"""Qwen3-TTS backend selection: faster_qwen3_tts (CUDA graph capture, real-time)
is used only when a real CUDA GPU is present -- it has no CPU/MPS fallback
(https://github.com/andimarafioti/faster-qwen3-tts). Everywhere else the
existing qwen_tts package (CPU/MPS/CUDA-capable) keeps being used unchanged.

faster_qwen3_tts is not installed in this test environment (CUDA-only, and
this suite runs on CPU/MPS CI/dev machines) -- selection-logic tests fake its
presence via sys.modules / monkeypatched module_available rather than
requiring the real package.
"""

import sys
import types

import pytest

from app.services.tts.providers import qwen3_tts_provider as mod
from app.schemas.tts import TTSRequest


@pytest.fixture(autouse=True)
def _clear_model_cache():
    mod._CACHE.clear()
    yield
    mod._CACHE.clear()


class TestUseFasterBackend:
    def test_false_when_not_cuda_even_if_package_installed(self, monkeypatch):
        monkeypatch.setattr(mod, "module_available", lambda name: True)
        assert mod._use_faster_backend("mps") is False
        assert mod._use_faster_backend("cpu") is False

    def test_false_on_cuda_when_package_missing(self, monkeypatch):
        monkeypatch.setattr(mod, "module_available", lambda name: False)
        assert mod._use_faster_backend("cuda:0") is False

    def test_true_only_when_cuda_and_package_installed(self, monkeypatch):
        monkeypatch.setattr(mod, "module_available", lambda name: name == "faster_qwen3_tts")
        assert mod._use_faster_backend("cuda:0") is True


class TestLoadModelBackendSelection:
    def test_uses_qwen_tts_on_mps(self, monkeypatch):
        monkeypatch.setattr(mod, "_pick_device_dtype_attn", lambda: ("mps", "float32-stub", None))
        monkeypatch.setattr(mod, "module_available", lambda name: True)  # even if "installed"

        calls = []

        class _FakeQwen3TTSModel:
            @classmethod
            def from_pretrained(cls, checkpoint, **kwargs):
                calls.append((checkpoint, kwargs))
                return "qwen-tts-model"

        fake_qwen_tts = types.ModuleType("qwen_tts")
        fake_qwen_tts.Qwen3TTSModel = _FakeQwen3TTSModel
        monkeypatch.setitem(sys.modules, "qwen_tts", fake_qwen_tts)

        provider = mod.Qwen3TTS06BProvider()
        model = provider._load_model("Base")

        assert model == "qwen-tts-model"
        assert calls == [("Qwen/Qwen3-TTS-12Hz-0.6B-Base", {"device_map": "mps", "dtype": "float32-stub"})]

    def test_uses_faster_qwen3_tts_on_cuda_when_installed(self, monkeypatch):
        monkeypatch.setattr(mod, "_pick_device_dtype_attn", lambda: ("cuda:0", "bf16-stub", "flash_attention_2"))
        monkeypatch.setattr(mod, "module_available", lambda name: name == "faster_qwen3_tts")

        calls = []
        warmed_up = []

        class _FakeModel:
            def warmup(self):
                warmed_up.append(True)

        class _FakeFasterQwen3TTS:
            @classmethod
            def from_pretrained(cls, checkpoint):
                calls.append(checkpoint)
                return _FakeModel()

        fake_module = types.ModuleType("faster_qwen3_tts")
        fake_module.FasterQwen3TTS = _FakeFasterQwen3TTS
        monkeypatch.setitem(sys.modules, "faster_qwen3_tts", fake_module)

        provider = mod.Qwen3TTS17BProvider()
        model = provider._load_model("CustomVoice")

        assert isinstance(model, _FakeModel)
        assert calls == ["Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"]
        assert warmed_up == [True], "warmup() must run once at load time, not on the first request"

    def test_falls_back_to_qwen_tts_on_cuda_when_faster_package_missing(self, monkeypatch):
        monkeypatch.setattr(mod, "_pick_device_dtype_attn", lambda: ("cuda:0", "bf16-stub", "flash_attention_2"))
        monkeypatch.setattr(mod, "module_available", lambda name: False)  # faster_qwen3_tts NOT installed

        calls = []

        class _FakeQwen3TTSModel:
            @classmethod
            def from_pretrained(cls, checkpoint, **kwargs):
                calls.append((checkpoint, kwargs))
                return "qwen-tts-model"

        fake_qwen_tts = types.ModuleType("qwen_tts")
        fake_qwen_tts.Qwen3TTSModel = _FakeQwen3TTSModel
        monkeypatch.setitem(sys.modules, "qwen_tts", fake_qwen_tts)

        provider = mod.Qwen3TTS06BProvider()
        model = provider._load_model("Base")

        assert model == "qwen-tts-model"
        assert calls[0][1]["attn_implementation"] == "flash_attention_2"


class TestGenerateVoiceCloneKwargs:
    def test_passes_x_vector_only_mode_on_qwen_tts_backend(self, monkeypatch):
        monkeypatch.setattr(mod, "_pick_device_dtype_attn", lambda: ("mps", "float32-stub", None))
        monkeypatch.setattr(mod, "module_available", lambda name: False)

        captured = {}

        class _FakeModel:
            def generate_voice_clone(self, **kwargs):
                captured.update(kwargs)
                return (["fake-audio"], 24000)

        provider = mod.Qwen3TTS06BProvider()
        monkeypatch.setattr(provider, "_load_model", lambda kind: _FakeModel())

        provider._generate(TTSRequest(
            text="hi", engine="qwen3_tts_0_6b", ref_audio_path="/ref.wav", ref_text="ref",
        ))

        assert captured["x_vector_only_mode"] is False

    def test_omits_x_vector_only_mode_on_faster_backend(self, monkeypatch):
        monkeypatch.setattr(mod, "_pick_device_dtype_attn", lambda: ("cuda:0", "bf16-stub", "flash_attention_2"))
        monkeypatch.setattr(mod, "module_available", lambda name: name == "faster_qwen3_tts")

        captured = {}

        class _FakeModel:
            def generate_voice_clone(self, **kwargs):
                captured.update(kwargs)
                return (["fake-audio"], 24000)

        provider = mod.Qwen3TTS06BProvider()
        monkeypatch.setattr(provider, "_load_model", lambda kind: _FakeModel())

        provider._generate(TTSRequest(
            text="hi", engine="qwen3_tts_0_6b", ref_audio_path="/ref.wav", ref_text="ref",
        ))

        assert "x_vector_only_mode" not in captured
