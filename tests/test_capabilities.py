"""GPU/CUDA detection should be robust: prefer torch.cuda when torch is loaded,
fall back to the NVIDIA driver/nvidia-smi (catches a Colab T4 either way)."""

import sys

import app.services.recommend.capabilities as cap


def test_cuda_true_when_torch_reports_available(monkeypatch):
    fake_torch = type("T", (), {"cuda": type("C", (), {"is_available": staticmethod(lambda: True)})})
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    assert cap._cuda() is True


def test_cuda_falls_back_to_nvidia_smi(monkeypatch):
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.setattr(cap.shutil, "which", lambda c: "/usr/bin/nvidia-smi" if c == "nvidia-smi" else None)
    monkeypatch.setattr(cap.os.path, "exists", lambda p: False)
    assert cap._cuda() is True


def test_cuda_false_when_no_gpu(monkeypatch):
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.setattr(cap.shutil, "which", lambda c: None)
    monkeypatch.setattr(cap.os.path, "exists", lambda p: False)
    assert cap._cuda() is False
