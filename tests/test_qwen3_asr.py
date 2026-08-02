"""Qwen3-ASR MLX STT engine: registered, Apple-only (auto-hidden), in catalog."""

import asyncio
import io
import sys
import threading
import time
import types
import wave

import pytest

import app.services.stt.providers.qwen3_asr_provider as q_mod
from app.services.recommend.catalog import CANDIDATES
from app.services.stt.providers.qwen3_asr_provider import Qwen3AsrProvider
from app.services.stt.service import stt_service


def _silent_wav() -> bytes:
    b = io.BytesIO()
    w = wave.open(b, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(16000)
    w.writeframes(b"\x00\x00" * 1600)
    w.close()
    return b.getvalue()


def test_registered():
    assert "qwen3_asr" in stt_service.providers
    assert isinstance(stt_service.providers["qwen3_asr"], Qwen3AsrProvider)


def test_mlx_backend_only_on_apple_silicon(monkeypatch):
    p = stt_service.providers["qwen3_asr"]
    monkeypatch.setattr(q_mod, "module_available", lambda m: m == "mlx_qwen3_asr")
    # Apple Silicon + mlx package -> mlx backend
    monkeypatch.setattr(q_mod, "_is_apple_silicon", lambda: True)
    assert p._backend() == "mlx"
    # NOT Apple (e.g. Colab Linux) — even with mlx_qwen3_asr installed, never load MLX
    # (libmlx.so doesn't exist on Linux); no other backend -> hidden, not a crash.
    monkeypatch.setattr(q_mod, "_is_apple_silicon", lambda: False)
    assert p._backend() is None


def test_non_apple_prefers_cuda_when_both_present(monkeypatch):
    p = stt_service.providers["qwen3_asr"]
    monkeypatch.setattr(q_mod, "_is_apple_silicon", lambda: False)
    # both mlx + qwen_asr installed on a Linux/CUDA host -> pick cuda, never mlx
    monkeypatch.setattr(q_mod, "module_available", lambda m: m in {"mlx_qwen3_asr", "qwen_asr"})
    assert p._backend() == "cuda"
    assert p.available() is True


def test_cuda_dtype_prefers_bf16_only_when_supported():
    class _Cuda:
        supported = False
        @classmethod
        def is_bf16_supported(cls):
            return cls.supported
    fake = type("T", (), {"bfloat16": "bf16", "float16": "fp16", "cuda": _Cuda})
    # T4 (Turing) — no bf16 -> float16
    _Cuda.supported = False
    assert q_mod._cuda_dtype(fake) == "fp16"
    # Ampere+ -> bfloat16
    _Cuda.supported = True
    assert q_mod._cuda_dtype(fake) == "bf16"


def test_torch_dtype_is_fp32_on_cpu_regardless_of_gpu_capability():
    """The torch backend also serves the CPU compose variant. Half precision on
    CPU raises "not implemented for 'Half'" inside the model, so the device --
    not the GPU's bf16 support -- decides the dtype."""
    class _Cuda:
        @staticmethod
        def is_bf16_supported():
            return True
    fake = type("T", (), {"bfloat16": "bf16", "float16": "fp16", "float32": "fp32", "cuda": _Cuda})
    assert q_mod._torch_dtype(fake, "cpu") == "fp32"
    assert q_mod._torch_dtype(fake, "cuda:0") == "bf16"


def test_cuda_model_cache_key_includes_the_device(monkeypatch):
    """A cpu-device container and a gpu one build different models from the same
    weights; a device-blind key would hand the second one the first one's model."""
    built: list[tuple] = []

    class FakeModel:
        @staticmethod
        def from_pretrained(model, dtype=None, device_map=None, max_new_tokens=None):
            built.append((model, dtype, device_map))
            return object()

    fake_torch = types.ModuleType("torch")
    fake_torch.float32 = "fp32"
    fake_torch.float16 = "fp16"
    fake_torch.bfloat16 = "bf16"
    fake_torch.cuda = types.SimpleNamespace(is_bf16_supported=lambda: True)
    fake_qwen = types.ModuleType("qwen_asr")
    fake_qwen.Qwen3ASRModel = FakeModel
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "qwen_asr", fake_qwen)
    q_mod._MODEL_CACHE.clear()

    p = Qwen3AsrProvider()
    monkeypatch.setenv("STT_QWEN3_ASR_DEVICE", "cpu")
    p._cuda_model("Qwen/Qwen3-ASR-0.6B")
    monkeypatch.setenv("STT_QWEN3_ASR_DEVICE", "cuda:0")
    p._cuda_model("Qwen/Qwen3-ASR-0.6B")

    assert [(d, dev) for _, d, dev in built] == [("fp32", "cpu"), ("bf16", "cuda:0")]
    q_mod._MODEL_CACHE.clear()


def test_neither_backend_hidden(monkeypatch):
    p = stt_service.providers["qwen3_asr"]
    monkeypatch.setattr(q_mod, "_is_apple_silicon", lambda: False)
    monkeypatch.setattr(q_mod, "module_available", lambda m: False)
    assert p._backend() is None
    assert p.available() is False


@pytest.mark.asyncio
async def test_listed_reflects_package_presence():
    from app.core.deps import module_available

    engines = {e["engine"]: e for e in await stt_service.list_engines()}
    assert "qwen3_asr" in engines
    assert engines["qwen3_asr"]["mode"] == "local"
    assert engines["qwen3_asr"]["available"] == module_available("mlx_qwen3_asr")


def test_stt_request_schema_accepts_qwen3_asr():
    from app.schemas.stt import STTRequest

    assert STTRequest(engine="qwen3_asr").engine == "qwen3_asr"


async def test_mlx_session_single_flight_and_thread_pinned(monkeypatch):
    """MLX is not safe for concurrent cross-thread use. The conversation warms STT
    in the background while the user speaks, so turn-1 transcribe can race the warm's
    Session build. Regression guard: the Session must be built exactly once and all
    MLX work must run on a single dedicated thread, or MLX segfaults / raises
    "There is no Stream(gpu, N) in current thread"."""
    builds: list[int] = []  # thread ids at Session construction
    txns: list[int] = []  # thread ids during transcribe

    class FakeSession:
        def __init__(self, model):
            builds.append(threading.get_ident())
            time.sleep(0.05)  # widen the race window

        def transcribe(self, path, language=None):
            txns.append(threading.get_ident())
            return types.SimpleNamespace(text="ok")

    fake_mod = types.ModuleType("mlx_qwen3_asr")
    fake_mod.Session = FakeSession
    monkeypatch.setitem(sys.modules, "mlx_qwen3_asr", fake_mod)
    monkeypatch.setattr(q_mod, "_is_apple_silicon", lambda: True)
    monkeypatch.setattr(q_mod, "module_available", lambda m: m == "mlx_qwen3_asr")
    q_mod._MODEL_CACHE.clear()

    p = Qwen3AsrProvider()
    wav = _silent_wav()
    # fire many concurrent first-time transcribes — they all hit the build path at once
    results = await asyncio.gather(*[p.transcribe_bytes(wav, "vi") for _ in range(8)])

    assert all(r.text == "ok" for r in results)
    assert len(builds) == 1, f"Session built {len(builds)}x — build is not single-flight"
    assert set(txns) == set(builds), "transcribe ran on a different thread than the build"
    assert len(set(txns)) == 1, "MLX work was not pinned to one dedicated thread"


def test_in_recommend_catalog_apple_and_cuda_vietnamese():
    cands = [x for x in CANDIDATES if x.engine == "qwen3_asr"]
    chips = {c.chip for c in cands}
    assert chips == {"apple_silicon", "nvidia_gpu"}, "expect both Apple + CUDA candidates"
    for c in cands:
        assert c.category == "stt"
        assert c.vietnamese is True  # Qwen3-ASR supports Vietnamese (verified on Apple)
    cuda = next(c for c in cands if c.chip == "nvidia_gpu")
    assert "qwen_asr" in cuda.requires
