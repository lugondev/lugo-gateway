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
    dict(id="whisper", label="Whisper — STT (CPU)", module="faster_whisper",
         install=("extra", "tts"), hosts={"apple", "nvidia", "cpu"}, note="base"),
    dict(id="vosk", label="Vosk — streaming STT (CPU)", module="vosk",
         install=("extra", "tts"), hosts={"apple", "nvidia", "cpu"}, note="base"),
    dict(id="qwen3_asr_cuda", label="Qwen3-ASR — STT (NVIDIA GPU/CUDA) ⭐ Vietnamese", module="qwen_asr",
         install=("extra", "qwen3-asr-cuda"), hosts={"nvidia"}, note=""),
    dict(id="qwen3_asr_mlx", label="Qwen3-ASR — STT (Apple MLX) ⭐ Vietnamese", module="mlx_qwen3_asr",
         install=("extra", "qwen3-asr"), hosts={"apple"}, note=""),
    dict(id="whisper_mlx", label="Whisper — STT (Apple MLX, ~7×)", module="mlx_whisper",
         install=("extra", "mlx"), hosts={"apple"}, note=""),
    dict(id="vieneu", label="VieNeu v3turbo — TTS (CPU, Vietnamese)", module="vieneu",
         install=("extra", "tts"), hosts={"apple", "nvidia", "cpu"}, note=""),
    dict(id="vieneu_gpu", label="VieNeu GPU modes — TTS (turbo/fast)", module="lmdeploy",
         install=("pip", "vieneu[gpu]"), hosts={"nvidia"}, note=""),
    dict(id="omnivoice", label="OmniVoice — TTS (600+ langs, voice clone)", module="omnivoice",
         install=("pip", "omnivoice"), hosts={"apple", "nvidia", "cpu"}, note=""),
    dict(id="voxcpm2", label="VoxCPM — TTS (30 langs, CPU/MPS/CUDA, clone)", module="voxcpm",
         install=("pip", "voxcpm"), hosts={"apple", "nvidia", "cpu"}, note=""),
    dict(id="kokoro_vi", label="Kokoro-Vietnamese — TTS (CPU, fixed voicepacks)",
         module="kokoro_vietnamese",
         install=("pip", "kokoro-vietnamese @ git+https://github.com/iamdinhthuan/Kokoro-Vietnamese.git"),
         hosts={"apple", "nvidia", "cpu"}, note=""),
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
        "ALLOW_RUNTIME_INSTALL": "true",     # enable the Install buttons in the UI
        "DEFAULT_TTS_ENGINE": "vieneu",
        "CONVERSATION_TTS_ENGINE": "vieneu",
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


def _apply(selected: list, host: str) -> None:
    """Install the selected components and persist runtime config to .env."""
    if not selected:
        print("Nothing selected.")
        return
    for cmd in install_commands(selected):
        print("+", " ".join(cmd))
        subprocess.run(cmd, check=False)
    cfg = build_env({c["id"] for c in selected}, host)
    write_env(cfg)
    print("\nWrote config to .env:")
    for k, v in cfg.items():
        print(f"  {k}={v}")
    print("\nDone — the gateway reads .env, just run (no env vars):")
    print("  PYTHONPATH=apps/api_gateway python -m uvicorn app.main:app --host 0.0.0.0 --port 8000")


def _in_notebook() -> bool:
    try:
        from IPython import get_ipython

        ip = get_ipython()
        return ip is not None and "IPKernelApp" in ip.config
    except Exception:  # noqa: BLE001
        return False


def _widget_checklist(host: str, rows: list) -> None:
    """ipywidgets checklist for notebooks/Colab — tick boxes, click to install."""
    import ipywidgets as wdg
    from IPython.display import display

    items, pairs = [], []
    for r in rows:
        if r["installed"]:
            items.append(wdg.HTML(f"✓ <b>{r['label']}</b> — <span style='color:#6ee7a8'>installed</span>"))
        else:
            cb = wdg.Checkbox(value=False, description=r["label"], indent=False)
            pairs.append((r["c"], cb))
            items.append(cb)
    btn = wdg.Button(description="Install & write .env", button_style="success")
    out = wdg.Output()

    def on_click(_):
        with out:
            out.clear_output()
            _apply([c for c, cb in pairs if cb.value], host)

    btn.on_click(on_click)
    display(wdg.VBox(
        [wdg.HTML(f"<b>Setup — host: {host}</b> (tick các engine chưa cài)")] + items + [btn, out]
    ))


def wizard() -> None:
    """Pick the best UI for the environment: ipywidgets (notebook) > curses (TTY) > list."""
    host = detect_host()
    rows = [{"c": c, "label": c["label"], "installed": is_installed(c)} for c in compatible_components(host)]
    if _in_notebook():
        try:
            _widget_checklist(host, rows)
            return
        except Exception as exc:  # noqa: BLE001 - ipywidgets missing -> fall through
            print(f"(widgets unavailable: {exc})")
    if sys.stdout.isatty():
        _apply(_checklist(host, rows), host)
        return
    print(f"Host: {host}. Installable components:")
    for r in rows:
        print(f"  {'[installed]' if r['installed'] else '[ ]'} {r['label']}")
    print("\nRun `make setup` in a terminal, or `import setup; setup.wizard()` in a notebook cell.")


def main() -> int:
    wizard()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
