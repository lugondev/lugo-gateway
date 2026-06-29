#!/usr/bin/env python3
"""Interactive setup wizard — a terminal checklist of engine packages.

Detects the host (Apple Silicon / NVIDIA GPU / CPU), shows only the components that
can run here, marks the already-installed ones, and lets you tick the rest with
SPACE (↑/↓ to move, ENTER to install, q to quit). Stdlib only (curses) — no extra
dependency, and self-contained so it runs even before the project is installed.

Falls back to a printed list + flag hints when there is no interactive TTY
(e.g. a Colab `!` cell) — use scripts/setup.sh there.

    python scripts/setup.py        # interactive checklist
    make setup
"""

import importlib.util
import os
import platform
import shutil
import subprocess
import sys

# Each component: how to detect it (module), how to install it, and which hosts it
# can run on. install is ("extra", name) -> folded into `pip install -e .[...]`,
# or ("pip", spec) -> `pip install <spec>`.
COMPONENTS = [
    dict(id="whisper", label="Whisper / PhoWhisper — STT (CPU)", module="faster_whisper",
         install=("extra", "tts"), hosts={"apple", "nvidia", "cpu"}, note="base"),
    dict(id="vosk", label="Vosk — streaming STT (CPU)", module="vosk",
         install=("extra", "tts"), hosts={"apple", "nvidia", "cpu"}, note="base"),
    dict(id="qwen3_asr_cuda", label="Qwen3-ASR — STT (NVIDIA GPU/CUDA) ⭐ Vietnamese", module="qwen_asr",
         install=("extra", "qwen3-asr-cuda"), hosts={"nvidia"}, note=""),
    dict(id="qwen3_asr_mlx", label="Qwen3-ASR — STT (Apple MLX) ⭐ Vietnamese", module="mlx_qwen3_asr",
         install=("extra", "qwen3-asr"), hosts={"apple"}, note=""),
    dict(id="whisper_mlx", label="PhoWhisper — STT (Apple MLX, ~7×)", module="mlx_whisper",
         install=("extra", "mlx"), hosts={"apple"}, note=""),
    dict(id="vieneu", label="VieNeu v3turbo — TTS (CPU, Vietnamese)", module="vieneu",
         install=("extra", "tts"), hosts={"apple", "nvidia", "cpu"}, note=""),
    dict(id="vieneu_gpu", label="VieNeu GPU modes — TTS (turbo/fast)", module="lmdeploy",
         install=("pip", "vieneu[gpu]"), hosts={"nvidia"}, note=""),
    dict(id="omnivoice", label="OmniVoice — TTS (600+ langs, voice clone)", module="omnivoice",
         install=("pip", "omnivoice"), hosts={"apple", "nvidia", "cpu"}, note=""),
    dict(id="silero", label="Silero VAD (CPU)", module="silero_vad",
         install=("pip", "silero-vad"), hosts={"apple", "nvidia", "cpu"}, note=""),
    dict(id="pyannote", label="pyannote VAD (gated)", module="pyannote.audio",
         install=("pip", "pyannote.audio"), hosts={"apple", "nvidia", "cpu"}, note=""),
    dict(id="opus", label="Opus transport (ESP32/RPi/browser)", module="opuslib",
         install=("extra", "opus"), hosts={"apple", "nvidia", "cpu"}, note=""),
]


def module_available(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except Exception:  # noqa: BLE001
        return False


def detect_host() -> str:
    if platform.system() == "Darwin" and platform.machine().lower() in {"arm64", "aarch64"}:
        return "apple"
    torch = sys.modules.get("torch")
    if torch is not None:
        try:
            if torch.cuda.is_available():
                return "nvidia"
        except Exception:  # noqa: BLE001
            pass
    if shutil.which("nvidia-smi") or os.path.exists("/proc/driver/nvidia/version"):
        return "nvidia"
    return "cpu"


def compatible_components(host: str) -> list:
    return [c for c in COMPONENTS if host in c["hosts"]]


def is_installed(component: dict) -> bool:
    return module_available(component["module"])


def build_env(selected_ids: set, host: str) -> dict:
    """Runtime config to persist to .env so the gateway needs no manual env vars."""
    env = {
        "ENABLE_MOCK_ENGINES": "false",      # real audio, not silent placeholders
        "ALLOW_RUNTIME_INSTALL": "true",     # enable the Install buttons in the UI
        "DEFAULT_TTS_ENGINE": "vieneu",
        "CONVERSATION_TTS_ENGINE": "vieneu",
        "CONVERSATION_AUDIO_NATIVE": "false",
    }
    if {"qwen3_asr_cuda", "qwen3_asr_mlx"} & selected_ids:
        env["CONVERSATION_STT_ENGINE"] = "qwen3_asr"
    else:
        env["CONVERSATION_STT_ENGINE"] = "whisper"
    if "omnivoice" in selected_ids:
        # Point OmniVoice at THIS interpreter (where we just pip-installed it) + a real
        # cwd for the sidecar, so available() resolves without the Mac-only default path.
        env["OMNIVOICE_PYTHON"] = sys.executable
        env["OMNIVOICE_PATH"] = os.getcwd()
        if host == "nvidia":
            env["OMNIVOICE_DEVICE"] = "cuda:0"
    return env


def write_env(updates: dict, path: str = ".env") -> None:
    """Merge KEY=VALUE updates into .env, preserving existing keys/comments."""
    lines = []
    if os.path.exists(path):
        with open(path) as f:
            lines = f.read().splitlines()
    seen, out = set(), []
    for ln in lines:
        if "=" in ln and not ln.lstrip().startswith("#"):
            key = ln.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out.append(ln)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")


def install_commands(selected: list) -> list:
    """Build pip commands: one `-e .[extras]` for all extras + one for raw pip specs."""
    py = os.environ.get("PYTHON", sys.executable)
    extras = sorted({c["install"][1] for c in selected if c["install"][0] == "extra"})
    pips = sorted({c["install"][1] for c in selected if c["install"][0] == "pip"})
    cmds = []
    if extras:
        cmds.append([py, "-m", "pip", "install", "-e", ".[" + ",".join(extras) + "]"])
    for spec in pips:
        cmds.append([py, "-m", "pip", "install", spec])
    return cmds


# --------------------------------------------------------------------------- TUI
def _checklist(host: str, rows: list) -> list:
    """Curses checklist; returns the selected (not-already-installed) components."""
    import curses

    selectable = [i for i, r in enumerate(rows) if not r["installed"]]
    checked = set()

    def draw(stdscr):
        curses.curs_set(0)
        cur = selectable[0] if selectable else 0
        while True:
            stdscr.clear()
            stdscr.addstr(0, 0, f"Setup — host: {host}.  SPACE tick · ↑/↓ move · ENTER install · q quit")
            for i, r in enumerate(rows):
                if r["installed"]:
                    mark, label = "[✓ installed]", r["label"]
                    attr = curses.A_DIM
                else:
                    mark = "[x]" if i in checked else "[ ]"
                    label = r["label"]
                    attr = curses.A_REVERSE if i == cur else curses.A_NORMAL
                stdscr.addstr(i + 2, 2, f"{mark} {label}", attr)
            stdscr.refresh()
            k = stdscr.getch()
            if k in (ord("q"), 27):
                return []
            if k in (curses.KEY_ENTER, 10, 13):
                return [rows[i]["c"] for i in sorted(checked)]
            if not selectable:
                continue
            if k == curses.KEY_UP:
                cur = selectable[(selectable.index(cur) - 1) % len(selectable)]
            elif k == curses.KEY_DOWN:
                cur = selectable[(selectable.index(cur) + 1) % len(selectable)]
            elif k == ord(" ") and cur in selectable:
                checked.symmetric_difference_update({cur})

    return curses.wrapper(draw)


def main() -> int:
    host = detect_host()
    comps = compatible_components(host)
    rows = [{"c": c, "label": c["label"], "installed": is_installed(c)} for c in comps]

    if not sys.stdout.isatty():
        # Non-interactive (e.g. Colab `!`): print the host-filtered list + how to install.
        print(f"Host: {host}. Installable components (use scripts/setup.sh flags or `make setup` in a TTY):")
        for r in rows:
            print(f"  {'[installed]' if r['installed'] else '[ ]'} {r['label']}")
        return 0

    selected = _checklist(host, rows)
    if not selected:
        print("Nothing selected.")
        return 0
    for cmd in install_commands(selected):
        print("+", " ".join(cmd))
        subprocess.run(cmd, check=False)
    # Persist runtime config to .env so the gateway needs no manual env vars.
    cfg = build_env({c["id"] for c in selected}, host)
    write_env(cfg)
    print("\nWrote config to .env:")
    for k, v in cfg.items():
        print(f"  {k}={v}")
    print("\nDone. Just run the gateway (it reads .env — no env vars needed):")
    print("  PYTHONPATH=apps/api_gateway python -m uvicorn app.main:app --host 0.0.0.0 --port 8000")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
