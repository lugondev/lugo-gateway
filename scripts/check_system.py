#!/usr/bin/env python3
"""Scan the machine and say what this project should run on it.

Three questions, one command:

1. **What is this box?** CPU/RAM/disk, Apple Silicon vs NVIDIA vs plain CPU, GPU
   model and VRAM, docker + NVIDIA Container Toolkit, audio system libs.
2. **Is anything wrong?** A pass/warn/fail list -- too little RAM or disk, a
   Python too old, a missing `libopus` (no device audio transport), an NVIDIA
   driver with no container toolkit (the -gpu compose files can't work), no
   STT/TTS engine installed at all.
3. **What should I run?** The engine stack for this host, and -- because every
   engine here ships as a one-service container -- exactly which
   `infra/compose/*.yml` file to deploy, CPU or GPU variant.

Stdlib only, so it runs on a bare box before anything is installed (same
constraint as scripts/setup.py). It deliberately reuses rather than re-derives:

- host classification + the installed-package inventory come from
  `scripts/setup.py` (loaded by path -- scripts/ is not a package),
- per-model advice comes from the gateway's own recommender
  (`app.services.recommend`) when the project is importable; without it that one
  section is skipped rather than reimplemented.

    python scripts/check_system.py            # human report
    python scripts/check_system.py --json     # same facts as JSON (for tooling)

Exit code is 1 when any check FAILs, 0 otherwise -- warnings never fail, so this
is safe in CI as a smoke check.
"""

from __future__ import annotations

import argparse
import ctypes.util
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

# Minimum Python the project declares (pyproject requires-python).
_MIN_PYTHON = (3, 10)
# Below this, nothing in the catalog fits; below the warn line the small models
# still work but the recommended ones don't.
_RAM_FAIL_GB = 2.0
_RAM_WARN_GB = 8.0
# A single whisper-large/qwen3-asr download is 1.5-3.5GB, and the docker images
# for the torch engines are ~3GB each.
_DISK_WARN_GB = 15.0


# --------------------------------------------------------------------- reuse
def load_setup_module():
    """Load scripts/setup.py by path (scripts/ is not a package).

    Gives us detect_host(), COMPONENTS and is_installed() instead of a second,
    silently-diverging copy of the same host rules.
    """
    path = _ROOT / "scripts" / "setup.py"
    spec = importlib.util.spec_from_file_location("_stt_setup_wizard", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_stt_setup_wizard"] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------- probes
def _run(cmd: list[str], timeout: float = 5.0) -> str | None:
    """Best-effort command output; None on anything going wrong.

    Every probe here is optional information -- a missing binary, a hung daemon
    or a permission error must degrade to "unknown", never crash the report.
    """
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def ram_total_gb() -> float | None:
    try:
        return round(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1024**3, 1)
    except (ValueError, OSError, AttributeError):
        pass
    try:  # containers/older Linux without the sysconf pair
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return round(int(line.split()[1]) / 1024**2, 1)
    except OSError:
        pass
    return None


def disk_free_gb(path: Path) -> float | None:
    try:
        return round(shutil.disk_usage(path).free / 1024**3, 1)
    except OSError:
        return None


def nvidia_gpus() -> list[dict]:
    """[{name, vram_gb, driver}] from nvidia-smi; empty when there's no NVIDIA GPU."""
    raw = _run([
        "nvidia-smi",
        "--query-gpu=name,memory.total,driver_version",
        "--format=csv,noheader,nounits",
    ])
    gpus = []
    for line in (raw or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            vram = round(float(parts[1]) / 1024, 1)  # MiB -> GiB
        except ValueError:
            vram = None
        gpus.append({"name": parts[0], "vram_gb": vram, "driver": parts[2]})
    return gpus


def docker_info() -> dict:
    """{installed, version, nvidia_runtime} -- nvidia_runtime is what the -gpu
    compose files' device reservation actually needs on the host."""
    if not shutil.which("docker"):
        return {"installed": False, "version": None, "nvidia_runtime": False}
    version = _run(["docker", "version", "--format", "{{.Server.Version}}"])
    runtimes = _run(["docker", "info", "--format", "{{json .Runtimes}}"], timeout=10.0) or ""
    return {
        "installed": True,
        "version": version,
        # `docker info` is the authoritative answer; the binary check is a fallback
        # for a daemon that isn't running right now (common on a fresh box).
        "nvidia_runtime": "nvidia" in runtimes
        or shutil.which("nvidia-container-runtime") is not None,
    }


# Where a native lib can sit while still being invisible to the dynamic loader.
# Homebrew's prefix is the one that bites on macOS: `brew install opus` puts
# libopus.dylib in /opt/homebrew/lib, which ctypes/dyld do NOT search by default,
# so opuslib raises "Could not find Opus library" on a machine where the library
# is very much installed. Reporting that as "not found" would send someone off to
# reinstall a package they already have.
_LIB_DIRS = (
    "/opt/homebrew/lib",
    "/usr/local/lib",
    "/usr/lib",
    "/lib/x86_64-linux-gnu",
    "/usr/lib/x86_64-linux-gnu",
    "/usr/lib/aarch64-linux-gnu",
)


def find_lib(name: str) -> dict:
    """{loadable, present, path} for a native library.

    loadable -> the dynamic loader finds it (ctypes/cffi bindings will work).
    present but not loadable -> installed somewhere off the loader path.
    """
    resolved = ctypes.util.find_library(name)
    if resolved:
        return {"loadable": True, "present": True, "path": resolved}
    for directory in _LIB_DIRS:
        for suffix in (".dylib", ".so", ".so.0"):
            candidate = Path(directory) / f"lib{name}{suffix}"
            if candidate.exists():
                return {"loadable": False, "present": True, "path": str(candidate)}
    return {"loadable": False, "present": False, "path": None}


def soundfile_state() -> dict:
    """Whether Python audio I/O actually works, independent of the system lib.

    The `soundfile` wheel bundles its own libsndfile (`_soundfile_data/`), so a
    box with no system libsndfile is perfectly fine as long as soundfile imports.
    Checking only the system lib would warn on every machine that installed
    soundfile from PyPI -- i.e. all of them. `opuslib`, by contrast, bundles
    nothing, which is why libopus is still checked on its own.
    """
    if importlib.util.find_spec("soundfile") is None:
        return {"importable": False, "libsndfile_version": None}
    try:
        import soundfile
    except Exception:  # noqa: BLE001 - a broken install is "not usable", not a crash
        return {"importable": False, "libsndfile_version": None}
    return {
        "importable": True,
        "libsndfile_version": getattr(soundfile, "__libsndfile_version__", None),
    }


def scan() -> dict:
    """Every hardware/software fact the checks and recommendations read."""
    setup = load_setup_module()
    host = setup.detect_host()  # apple | nvidia | cpu
    gpus = nvidia_gpus()
    return {
        "host": host,
        "os": platform.system(),
        "os_release": platform.release(),
        "arch": platform.machine(),
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "cpu_count": os.cpu_count(),
        "ram_gb": ram_total_gb(),
        "disk_free_gb": disk_free_gb(_ROOT),
        "apple_silicon": host == "apple",
        "nvidia_gpus": gpus,
        "vram_gb": max((g["vram_gb"] or 0 for g in gpus), default=None) or None,
        "docker": docker_info(),
        "libopus": find_lib("opus"),
        "libsndfile": find_lib("sndfile"),
        "soundfile": soundfile_state(),
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ollama": shutil.which("ollama") is not None,
        "packages": {c["id"]: setup.is_installed(c) for c in setup.COMPONENTS},
        "packages_compatible": [c["id"] for c in setup.compatible_components(host)],
    }


# ------------------------------------------------------------------- checks
def _check(level: str, title: str, detail: str, fix: str = "") -> dict:
    return {"level": level, "title": title, "detail": detail, "fix": fix}


def checks(facts: dict) -> list[dict]:
    """Pass/warn/fail list. Pure: takes scan() output, returns findings."""
    out = [_python_check(facts), _ram_check(facts), _disk_check(facts)]
    out.append(_engine_check(facts, ("whisper", "vosk", "qwen3_asr_cuda", "qwen3_asr_mlx",
                                     "whisper_mlx"), "STT"))
    out.append(_engine_check(facts, ("vieneu", "omnivoice", "voxcpm2", "kokoro_vi"), "TTS"))
    out.append(_libopus_check(facts))
    out.append(_libsndfile_check(facts))
    out.extend(_gpu_checks(facts))
    return out


def _python_check(facts: dict) -> dict:
    current = tuple(int(p) for p in facts["python"].split(".")[:2])
    wanted = ".".join(str(p) for p in _MIN_PYTHON)
    if current < _MIN_PYTHON:
        return _check("fail", "Python version", f"{facts['python']} is below {wanted}",
                      f"Use Python {wanted}+ (the ML wheels this project needs stop at 3.13)")
    return _check("ok", "Python version", f"{facts['python']} (>= {wanted})")


def _ram_check(facts: dict) -> dict:
    ram = facts["ram_gb"]
    if ram is None:
        return _check("warn", "RAM", "could not be detected",
                      "Check manually; the recommended models want ~8 GB")
    if ram < _RAM_FAIL_GB:
        return _check("fail", "RAM", f"{ram} GB — below the {_RAM_FAIL_GB} GB floor",
                      "Nothing in the model catalog fits; use a remote/cloud engine instead")
    if ram < _RAM_WARN_GB:
        return _check("warn", "RAM", f"{ram} GB — under {_RAM_WARN_GB} GB",
                      "Stick to the small models (whisper base/small, vosk); large-v3 wants ~6 GB")
    return _check("ok", "RAM", f"{ram} GB")


def _disk_check(facts: dict) -> dict:
    free = facts["disk_free_gb"]
    if free is None:
        return _check("warn", "Disk", "free space could not be detected")
    if free < _DISK_WARN_GB:
        return _check("warn", "Disk", f"{free} GB free — under {_DISK_WARN_GB} GB",
                      "One model download is 1.5-3.5 GB and a torch docker image ~3 GB")
    return _check("ok", "Disk", f"{free} GB free")


def _engine_check(facts: dict, ids: tuple, kind: str) -> dict:
    installed = [i for i in ids if facts["packages"].get(i)]
    if installed:
        return _check("ok", f"{kind} engine installed", ", ".join(installed))
    return _check("warn", f"{kind} engine installed", "none found",
                  "Run `bash scripts/setup.sh` (or `python scripts/setup.py`) to install one")


def _native_lib_check(lib: dict, title: str, needed_for: str, install: str) -> dict:
    """Three outcomes, not two: installed-and-loadable, installed-but-off-the-
    loader-path (a different fix entirely), or genuinely absent."""
    if lib["loadable"]:
        return _check("ok", title, f"found ({lib['path']})")
    if lib["present"]:
        directory = str(Path(lib["path"]).parent)
        return _check(
            "warn", title, f"installed at {lib['path']} but the dynamic loader can't find it",
            f"{needed_for} Export DYLD_FALLBACK_LIBRARY_PATH={directory} (macOS) or add it to "
            "LD_LIBRARY_PATH / ldconfig (Linux) — reinstalling won't help. The repo's Makefile "
            "already exports that path, so anything started via `make` is unaffected; this only "
            "bites a bare `python ...` shell.",
        )
    return _check("warn", title, "not found", f"{needed_for} {install}")


def _libopus_check(facts: dict) -> dict:
    return _native_lib_check(
        facts["libopus"], "libopus",
        "ESP32/RPi/browser clients send Opus; without it that transport is dead.",
        "brew install opus  |  apt-get install libopus0",
    )


def _libsndfile_check(facts: dict) -> dict:
    sf = facts["soundfile"]
    if sf["importable"]:
        # The wheel's bundled copy is the normal case; say which one is in play so
        # nobody "fixes" a system lib that isn't being used.
        version = sf["libsndfile_version"] or "unknown version"
        source = "system" if facts["libsndfile"]["loadable"] else "bundled with the wheel"
        return _check("ok", "Audio I/O (soundfile)", f"libsndfile {version} ({source})")
    return _native_lib_check(
        facts["libsndfile"], "Audio I/O (soundfile)",
        "TTS audio I/O needs the soundfile package (it bundles libsndfile).",
        'pip install soundfile  (or the project extras: pip install -e ".[tts]")',
    )


def _gpu_checks(facts: dict) -> list[dict]:
    """GPU findings, including the one that silently breaks the -gpu compose files."""
    out = []
    if facts["host"] == "apple":
        out.append(_check("ok", "Accelerator", "Apple Silicon (Metal) — MLX engines available"))
        # Not a warning: it's a property of the platform, but it decides how this
        # host may deploy, so it must be visible.
        out.append(_check("ok", "Container note",
                          "MLX/Metal is invisible inside Docker — run the local engines natively"))
        return out
    if facts["host"] == "nvidia":
        gpus = facts["nvidia_gpus"]
        label = ", ".join(
            f"{g['name']} ({g['vram_gb']} GB, driver {g['driver']})" for g in gpus
        ) or "NVIDIA driver present (nvidia-smi reported nothing)"
        out.append(_check("ok", "Accelerator", label))
        docker = facts["docker"]
        if docker["installed"] and not docker["nvidia_runtime"]:
            out.append(_check(
                "warn", "NVIDIA Container Toolkit", "not detected",
                "The -gpu compose files reserve an nvidia device; without the toolkit the "
                "container starts with no GPU. Install nvidia-container-toolkit.",
            ))
        elif docker["installed"]:
            out.append(_check("ok", "NVIDIA Container Toolkit", "available to docker"))
        return out
    out.append(_check("ok", "Accelerator", "none — CPU only"))
    return out


# ----------------------------------------------------------- recommendation
# What to run per host class, and which one-service compose file deploys it.
# Kept as data (not prose in the renderer) so the JSON output, the text report
# and the tests all read the same table.
STACK = {
    "apple": {
        "stt": "qwen3_asr (MLX) — Metal GPU, strongest Vietnamese of the local engines",
        "tts": "vieneu (v3turbo) for realtime; omnivoice/voxcpm2 for cloning",
        "deploy": "native",
        "why": "MLX and Metal are invisible inside a Linux container, so the fast "
               "engines only exist outside Docker on this host.",
        "install": 'pip install -e ".[mlx,qwen3-asr,tts,opus]"',
        "compose": [],
    },
    "nvidia": {
        "stt": "qwen3_asr 1.7B on CUDA — or whisper_local large-v3-turbo (float16)",
        "tts": "omnivoice (RTF ~1 on GPU) — or qwen3_tts with the CUDA-graph fast path",
        "deploy": "docker-gpu",
        "why": "One container per engine, each reserving the GPU; needs the NVIDIA "
               "Container Toolkit on the host.",
        "install": 'pip install -e ".[qwen3-asr-cuda,tts]"  (native), or build the images below',
        "compose": [
            "infra/compose/docker-compose.stt-qwen3-asr-gpu.yml",
            "infra/compose/docker-compose.stt-whisper-turbo-gpu.yml",
            "infra/compose/docker-compose.tts-omnivoice-gpu.yml",
            "infra/compose/docker-compose.tts-qwen3-tts-gpu.yml",
            "infra/compose/docker-compose.tts-vieneu-gpu.yml",
            "infra/compose/docker-compose.tts-voxcpm2-gpu.yml",
        ],
    },
    "cpu": {
        "stt": "qwen3_asr_gguf (quantized C++ runtime) — or whisper_local large-v3-turbo @ int8",
        "tts": "vieneu (v3turbo/ONNX) — the only local engine that keeps up on CPU",
        "deploy": "docker-cpu",
        "why": "The torch engines run here but far from realtime; the quantized and "
               "ONNX paths are what make CPU-only usable.",
        "install": 'pip install -e ".[whisper,tts,opus]"',
        "compose": [
            "infra/compose/docker-compose.stt-qwen3-asr-gguf.yml",
            "infra/compose/docker-compose.stt-whisper-turbo-cpu.yml",
            "infra/compose/docker-compose.tts-vieneu.yml",
        ],
    },
}


def recommend(facts: dict) -> dict:
    """The stack for this host, plus the compose files that deploy it.

    `missing_compose` exists because the recommendation names files by path: if
    one is ever renamed or removed, the report says so instead of handing out a
    command that fails.
    """
    rec = dict(STACK[facts["host"]])
    rec["host"] = facts["host"]
    rec["missing_compose"] = [p for p in rec["compose"] if not (_ROOT / p).exists()]
    return rec


def model_recommendations(limit: int = 5) -> list | None:
    """Top-ranked models from the gateway's own recommender, or None when the
    project isn't importable (bare box, pre-install) -- the report degrades to
    the host-level advice above instead of duplicating the scoring here."""
    sys.path.insert(0, str(_ROOT / "apps" / "api_gateway"))
    sys.path.insert(0, str(_ROOT / "apps"))
    try:
        from app.services.recommend.capabilities import detect_capabilities
        from app.services.recommend.catalog import CANDIDATES
        from app.services.recommend.recommender import rank
    except Exception:  # noqa: BLE001 - any import failure means "not available here"
        return None
    try:
        ranked = rank(CANDIDATES, detect_capabilities(), installed_ids=set())
    except Exception:  # noqa: BLE001
        return None
    return [r for r in ranked if r["recommended"]][:limit]


# ------------------------------------------------------------------ rendering
_LEVEL_MARK = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}


def render(facts: dict, findings: list, rec: dict, models: list | None) -> str:
    lines = []
    add = lines.append

    add("==> Hardware")
    add(f"  Host class   {facts['host']}  ({facts['os']} {facts['os_release']} / {facts['arch']})")
    add(f"  CPU / RAM    {facts['cpu_count']} cores · {facts['ram_gb']} GB")
    add(f"  Disk free    {facts['disk_free_gb']} GB (repo volume)")
    if facts["nvidia_gpus"]:
        for gpu in facts["nvidia_gpus"]:
            add(f"  GPU          {gpu['name']} · {gpu['vram_gb']} GB VRAM · driver {gpu['driver']}")
    elif facts["apple_silicon"]:
        add("  GPU          Apple Silicon (Metal/MLX)")
    else:
        add("  GPU          none detected")
    docker = facts["docker"]
    docker_txt = (
        f"{docker['version'] or 'installed'}"
        + (" · nvidia runtime" if docker["nvidia_runtime"] else "")
        if docker["installed"]
        else "not installed"
    )
    add(f"  Docker       {docker_txt}")
    add(f"  Python       {facts['python']}  ({facts['python_executable']})")
    def lib_state(lib: dict) -> str:
        return "yes" if lib["loadable"] else ("installed, unloadable" if lib["present"] else "no")

    add(
        "  System libs  "
        f"libopus: {lib_state(facts['libopus'])} · "
        f"soundfile: {'yes' if facts['soundfile']['importable'] else 'no'} · "
        f"ffmpeg: {'yes' if facts['ffmpeg'] else 'no'} · "
        f"ollama: {'yes' if facts['ollama'] else 'no'}"
    )

    add("")
    add("==> Engine packages (installable on this host)")
    for pid in facts["packages_compatible"]:
        mark = "x" if facts["packages"].get(pid) else " "
        add(f"  [{mark}] {pid}")

    add("")
    add("==> Checks")
    for f in findings:
        add(f"  {_LEVEL_MARK[f['level']]}  {f['title']}: {f['detail']}")
        if f["fix"] and f["level"] != "ok":
            add(f"        -> {f['fix']}")

    add("")
    add(f"==> Recommended for this host ({rec['host']})")
    add(f"  STT     {rec['stt']}")
    add(f"  TTS     {rec['tts']}")
    add(f"  Deploy  {rec['deploy']} — {rec['why']}")
    add(f"  Install {rec['install']}")
    if rec["compose"]:
        add("  Compose files:")
        for path in rec["compose"]:
            missing = " (MISSING!)" if path in rec["missing_compose"] else ""
            add(f"    - {path}{missing}")
        add("    Build one with: SERVICE_API_TOKEN=... docker compose \\")
        add(f"      -f {rec['compose'][0]} --project-directory . up -d --build")

    if models:
        add("")
        add("==> Top models the gateway recommender would pick here")
        for m in models:
            add(f"  {m['category']:<4} {m['label']} ({m['size_estimate']}) — {m['reason']}")

    worst = "fail" if any(f["level"] == "fail" for f in findings) else (
        "warn" if any(f["level"] == "warn" for f in findings) else "ok"
    )
    add("")
    add(f"==> Result: {worst.upper()}")
    return "\n".join(lines)


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan this machine and recommend an engine stack.")
    parser.add_argument("--json", action="store_true", help="emit the raw facts as JSON")
    parser.add_argument("--no-models", action="store_true",
                        help="skip the gateway recommender section (faster, no app import)")
    args = parser.parse_args(argv)

    facts = scan()
    findings = checks(facts)
    rec = recommend(facts)
    models = None if args.no_models else model_recommendations()

    if args.json:
        print(json.dumps(
            {"hardware": facts, "checks": findings, "recommendation": rec, "models": models},
            indent=2,
        ))
    else:
        print(render(facts, findings, rec, models))

    return 1 if any(f["level"] == "fail" for f in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
