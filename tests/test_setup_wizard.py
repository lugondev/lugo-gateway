"""Pure logic of the interactive setup wizard (scripts/setup.py): host detection,
host-compatibility filtering, installed-status, and the pip install plan. The curses
UI itself is not unit-tested (needs a TTY)."""

import importlib.util
import sys
from pathlib import Path

# scripts/ is not a package — load setup.py directly.
_SETUP = Path(__file__).resolve().parents[1] / "scripts" / "setup.py"
_spec = importlib.util.spec_from_file_location("colab_setup_wizard", _SETUP)
setup = importlib.util.module_from_spec(_spec)
sys.modules["colab_setup_wizard"] = setup
_spec.loader.exec_module(setup)


def test_components_filtered_by_host():
    nvidia = {c["id"] for c in setup.compatible_components("nvidia")}
    apple = {c["id"] for c in setup.compatible_components("apple")}
    cpu = {c["id"] for c in setup.compatible_components("cpu")}
    # CUDA STT only on nvidia; MLX engines only on apple
    assert "qwen3_asr_cuda" in nvidia and "qwen3_asr_cuda" not in apple and "qwen3_asr_cuda" not in cpu
    assert "qwen3_asr_mlx" in apple and "qwen3_asr_mlx" not in nvidia
    assert "vieneu_gpu" in nvidia and "vieneu_gpu" not in cpu
    # vieneu (CPU) available everywhere
    for s in (nvidia, apple, cpu):
        assert "vieneu" in s


def test_installed_status_tracks_module(monkeypatch):
    monkeypatch.setattr(setup, "module_available", lambda m: m == "vieneu")
    vieneu = next(c for c in setup.COMPONENTS if c["id"] == "vieneu")
    silero = next(c for c in setup.COMPONENTS if c["id"] == "silero")
    assert setup.is_installed(vieneu) is True
    assert setup.is_installed(silero) is False


def test_install_plan_groups_extras_and_pips():
    cuda = next(c for c in setup.COMPONENTS if c["id"] == "qwen3_asr_cuda")  # extra
    gpu_tts = next(c for c in setup.COMPONENTS if c["id"] == "vieneu_gpu")    # raw pip
    cmds = setup.install_commands([cuda, gpu_tts])
    joined = " ".join(" ".join(c) for c in cmds)
    assert "qwen3-asr-cuda" in joined  # extra folded into pip install -e .[...]
    assert "vieneu[gpu]" in joined     # raw pip spec
