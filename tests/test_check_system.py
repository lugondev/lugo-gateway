"""Pure logic of the system scanner (scripts/check_system.py): the check rules,
the per-host recommendation table, and the JSON/exit-code contract the
system-check skill depends on. The probes themselves (nvidia-smi, docker,
sysconf) are not unit-tested -- they're environment reads; every test here feeds
synthetic facts instead."""

import importlib.util
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

# scripts/ is not a package — load check_system.py directly (same as test_setup_wizard).
_SPEC = importlib.util.spec_from_file_location(
    "stt_check_system", _ROOT / "scripts" / "check_system.py"
)
check_system = importlib.util.module_from_spec(_SPEC)
sys.modules["stt_check_system"] = check_system
_SPEC.loader.exec_module(check_system)


def _facts(**overrides) -> dict:
    base = {
        "host": "cpu",
        "os": "Linux",
        "os_release": "6.1",
        "arch": "x86_64",
        "python": "3.12.3",
        "python_executable": "/usr/bin/python3",
        "cpu_count": 8,
        "ram_gb": 16.0,
        "disk_free_gb": 100.0,
        "apple_silicon": False,
        "nvidia_gpus": [],
        "vram_gb": None,
        "docker": {"installed": True, "version": "27.0", "nvidia_runtime": False},
        "libopus": {"loadable": True, "present": True, "path": "/usr/lib/libopus.so.0"},
        "libsndfile": {"loadable": True, "present": True, "path": "/usr/lib/libsndfile.so.1"},
        "soundfile": {"importable": True, "libsndfile_version": "1.2.2"},
        "ffmpeg": True,
        "ollama": False,
        "packages": {"whisper": True, "vieneu": True},
        "packages_compatible": ["whisper", "vieneu"],
    }
    base.update(overrides)
    return base


def _by_title(findings: list, prefix: str) -> dict:
    return next(f for f in findings if f["title"].startswith(prefix))


def test_healthy_host_has_no_failures():
    findings = check_system.checks(_facts())
    assert [f for f in findings if f["level"] == "fail"] == []


def test_old_python_fails():
    finding = _by_title(check_system.checks(_facts(python="3.9.18")), "Python")
    assert finding["level"] == "fail"


def test_ram_thresholds():
    assert _by_title(check_system.checks(_facts(ram_gb=1.0)), "RAM")["level"] == "fail"
    assert _by_title(check_system.checks(_facts(ram_gb=4.0)), "RAM")["level"] == "warn"
    assert _by_title(check_system.checks(_facts(ram_gb=32.0)), "RAM")["level"] == "ok"


def test_installed_but_unloadable_libopus_is_not_reported_as_missing():
    """The Homebrew/dyld trap: libopus.dylib exists but ctypes can't load it, so
    opuslib dies with "Could not find Opus library". Telling someone to reinstall
    it is the wrong fix -- the message must point at the loader path."""
    facts = _facts(libopus={"loadable": False, "present": True,
                            "path": "/opt/homebrew/lib/libopus.dylib"})
    finding = _by_title(check_system.checks(facts), "libopus")
    assert finding["level"] == "warn"
    assert "/opt/homebrew/lib/libopus.dylib" in finding["detail"]
    assert "DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib" in finding["fix"]
    assert "reinstall" in finding["fix"]  # explicitly says reinstalling won't help


def test_soundfile_wheel_satisfies_libsndfile():
    """soundfile bundles its own libsndfile, so a missing system lib is a
    non-event; warning about it would fire on essentially every machine."""
    facts = _facts(libsndfile={"loadable": False, "present": False, "path": None})
    finding = _by_title(check_system.checks(facts), "Audio I/O")
    assert finding["level"] == "ok"
    assert "bundled" in finding["detail"]


def test_missing_soundfile_package_warns():
    facts = _facts(
        soundfile={"importable": False, "libsndfile_version": None},
        libsndfile={"loadable": False, "present": False, "path": None},
    )
    assert _by_title(check_system.checks(facts), "Audio I/O")["level"] == "warn"


def test_no_engine_installed_warns():
    facts = _facts(packages={"whisper": False, "vieneu": False})
    assert _by_title(check_system.checks(facts), "STT engine")["level"] == "warn"
    assert _by_title(check_system.checks(facts), "TTS engine")["level"] == "warn"


def test_nvidia_host_without_container_toolkit_warns():
    """The -gpu compose files reserve an nvidia device; without the toolkit the
    container comes up with no GPU and the engine silently runs on CPU (or dies)."""
    facts = _facts(
        host="nvidia",
        nvidia_gpus=[{"name": "NVIDIA A10G", "vram_gb": 22.5, "driver": "550.54"}],
        docker={"installed": True, "version": "27.0", "nvidia_runtime": False},
    )
    finding = _by_title(check_system.checks(facts), "NVIDIA Container Toolkit")
    assert finding["level"] == "warn"

    facts["docker"]["nvidia_runtime"] = True
    assert _by_title(check_system.checks(facts), "NVIDIA Container Toolkit")["level"] == "ok"


def test_recommendation_matches_host_and_names_real_compose_files():
    for host in ("apple", "nvidia", "cpu"):
        rec = check_system.recommend(_facts(host=host))
        assert rec["host"] == host
        assert rec["stt"] and rec["tts"] and rec["deploy"]
        # Every path the report prints must exist, or it hands out a broken command.
        assert rec["missing_compose"] == [], f"{host} recommends missing files"


def test_apple_recommends_native_and_gpu_host_recommends_gpu_compose():
    assert check_system.recommend(_facts(host="apple"))["deploy"] == "native"
    assert check_system.recommend(_facts(host="apple"))["compose"] == []
    nvidia = check_system.recommend(_facts(host="nvidia"))
    assert nvidia["deploy"] == "docker-gpu"
    assert all("-gpu.yml" in p for p in nvidia["compose"])
    cpu = check_system.recommend(_facts(host="cpu"))
    assert cpu["deploy"] == "docker-cpu"
    assert not any("-gpu.yml" in p for p in cpu["compose"])


def test_render_is_plain_text_and_mentions_the_result():
    facts = _facts()
    text = check_system.render(facts, check_system.checks(facts), check_system.recommend(facts), None)
    assert "==> Hardware" in text and "==> Checks" in text and "==> Result:" in text


def test_json_mode_shape_and_exit_code(capsys, monkeypatch):
    """The skill consumes this JSON — keep the top-level keys and the
    fail-implies-exit-1 contract pinned."""
    monkeypatch.setattr(check_system, "scan", lambda: _facts(python="3.9.1"))
    code = check_system.main(["--json", "--no-models"])
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"hardware", "checks", "recommendation", "models"}
    assert code == 1  # a FAIL check must be a non-zero exit

    monkeypatch.setattr(check_system, "scan", lambda: _facts())
    assert check_system.main(["--json", "--no-models"]) == 0
