---
name: system-check
description: Scan this machine's hardware and tell the user what this project can run on it — which STT/TTS engine, which infra/compose file (CPU vs GPU), and what's missing or misconfigured. Use when asked "what can this box run", "check/validate my system", "is my setup ok", "which engine should I use here", "why is opus/CUDA/the GPU not working", "should I deploy the cpu or gpu compose file", or before recommending an install/deploy on an unfamiliar host.
---

# System check

`scripts/check_system.py` is the single source of truth for "what is this box and
what should it run". It reports hardware, a pass/warn/fail validation list, and
the engine stack + `infra/compose/*.yml` file for the detected host class
(`apple` / `nvidia` / `cpu`).

## How to run it

```bash
.venv/bin/python scripts/check_system.py --json     # preferred: structured
python3 scripts/check_system.py --json              # bare box, no venv yet
```

- The script is **stdlib-only**, so it works before anything is installed.
- Add `--no-models` if the run is slow or the project isn't importable — that
  section (the gateway's own model recommender) is the only part that needs the
  app on `PYTHONPATH`, and it degrades to `null` on its own anyway.
- Exit code 1 means at least one **FAIL**; warnings exit 0.
- `make check-system` prints the human-readable version of the same thing.

## Rules

1. **Run the script; do not hand-roll the scan.** No ad-hoc `sysctl`,
   `nvidia-smi`, `free -h`, or `pip list` archaeology — every fact you need is in
   the JSON, and the script is what stays correct as engines change.
2. **Do not install or deploy anything without being asked.** This skill
   diagnoses and recommends; `scripts/setup.sh` / `docker compose` are the user's
   call.
3. If the JSON is missing a fact you need (an engine or a host type the script
   doesn't cover yet), extend `scripts/check_system.py` — the host→stack table is
   `STACK`, the rules are the `_*_check` functions, both covered by
   `tests/test_check_system.py`. Don't work around it in the answer.

## Reading the JSON

| Key | What to do with it |
|---|---|
| `hardware.host` | `apple` \| `nvidia` \| `cpu` — decides everything below |
| `hardware.nvidia_gpus[].vram_gb` | Whether the 1.7B/large models fit, or to suggest the smaller ones |
| `hardware.docker.nvidia_runtime` | `false` on an NVIDIA host = the `-gpu` compose files can't see the GPU |
| `checks[]` | `level` ∈ ok/warn/fail, each with `title`, `detail`, `fix` |
| `recommendation` | `stt`, `tts`, `deploy`, `install`, `compose[]` for this host |
| `recommendation.missing_compose` | Non-empty = the table names a file that no longer exists; say so |
| `models` | Top picks from the gateway's own recommender, or `null` when unavailable |

## Answering

Lead with the verdict, then the evidence. Keep it short:

1. **One line on the box** — host class, CPU/RAM, GPU (name + VRAM), Docker.
2. **Problems only** — every `fail` first, then `warn`, each with its `fix`.
   Don't recite the `ok` lines; say "the rest passed".
3. **Recommendation** — the STT/TTS engines and, for a docker deploy, the exact
   command with the right compose file:
   ```bash
   SERVICE_API_TOKEN=... docker compose \
     -f infra/compose/<file>.yml --project-directory . up -d --build
   ```
   The `--project-directory .` is not optional — these files use
   `build.context: "."` (repo root) to match how Coolify invokes compose.
4. **Answer in the language the user asked in.**

Two traps worth calling out explicitly when they appear, because both look like
"the library is missing" and are not:

- **libopus present but unloadable** — Homebrew installs it where dyld doesn't
  look. The fix is `DYLD_FALLBACK_LIBRARY_PATH`, not a reinstall; the repo's
  Makefile already exports it, so `make`-started processes are fine.
- **NVIDIA driver but no container toolkit** — the container starts, reserves
  nothing, and the engine quietly runs on CPU (or fails to load CUDA).
