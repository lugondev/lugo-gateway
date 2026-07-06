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


def test_omnivoice_is_a_pip_component():
    omni = next((c for c in setup.COMPONENTS if c["id"] == "omnivoice"), None)
    assert omni is not None and omni["install"] == ("pip", "omnivoice")


def test_extra_tts_engines_are_pip_components():
    by_id = {c["id"]: c for c in setup.COMPONENTS}
    assert by_id["voxcpm2"]["install"] == ("pip", "voxcpm") and by_id["voxcpm2"]["module"] == "voxcpm"
    assert "kittentts" not in by_id and "sherpa_onnx" not in by_id  # removed
    # Kokoro is MLX -> Apple only
    assert by_id["kokoro"]["hosts"] == {"apple"}


def test_build_env_picks_engines_and_omnivoice_python():
    env = setup.build_env({"qwen3_asr_cuda", "omnivoice"}, "nvidia")
    assert env["CONVERSATION_STT_ENGINE"] == "qwen3_asr"
    assert env["OMNIVOICE_PYTHON"]  # set so OmniVoice resolves in the gateway's python
    assert env.get("OMNIVOICE_DEVICE") == "cuda:0"
    # whisper-only selection -> STT falls back to whisper
    env2 = setup.build_env(set(), "cpu")
    assert env2["CONVERSATION_STT_ENGINE"] == "whisper"
    assert "OMNIVOICE_PYTHON" not in env2


def test_write_env_merges_preserving_existing(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# comment\nFOO=1\nSOME_KEY=old\n")
    setup.write_env({"SOME_KEY": "new", "NEW_KEY": "x"}, str(p))
    text = p.read_text()
    assert "FOO=1" in text                # untouched key preserved
    assert "# comment" in text            # comment preserved
    assert "SOME_KEY=new" in text         # updated in place
    assert "SOME_KEY=old" not in text
    assert "NEW_KEY=x" in text            # new key appended
