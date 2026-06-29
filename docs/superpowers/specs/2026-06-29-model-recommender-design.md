# Model Recommender — Design

Date: 2026-06-29
Status: Approved

## Problem

The System tab lets users download STT/TTS/LLM/VAD models, but gives no guidance
on **which** models suit the machine the gateway runs on. On the Coolify Linux
(x86_64, CPU-only) deploy, the Apple-only engines (`whisper_mlx`, `qwen_omni`)
cannot run at all, yet they appear in the catalog with no warning. Users need a
config-aware recommendation that ranks every downloadable model by how well it
fits the current hardware, and that also makes clear what each *other* chip class
would need.

## Goal

A **read-only** recommendation service that introspects the running container's
capabilities and returns, per category, a ranked list of all candidate models with
a fit score, runnable status, the reason, an estimated size, and the action
(existing download endpoint or install/config hint) needed to obtain it.

Non-goals: no auto-download, no one-click "install all", no background fetch. The
existing per-model download endpoints remain the only mutation path.

## Capabilities (detected from the running process)

`detect_capabilities() -> Capabilities` — stdlib only, every probe wrapped so it
never raises. Unknown values are `None` and are treated as "unknown" (never used to
mark a model incompatible — only a *definitively failed* hard requirement does that).

| field | source | notes |
|-------|--------|-------|
| `os` | `platform.system().lower()` | `darwin` / `linux` / ... |
| `arch` | `platform.machine()` | `arm64`/`aarch64`/`x86_64` |
| `apple_silicon` | `os == darwin and arch in {arm64, aarch64}` | |
| `cpu_count` | `os.cpu_count()` | |
| `ram_total_gb` | `/proc/meminfo` (linux), `sysctl hw.memsize` (darwin) | `None` if unreadable |
| `disk_free_gb` | `shutil.disk_usage(models_dir)` | |
| `mlx` | `module_available("mlx_whisper") or module_available("mlx_vlm")` | Apple GPU stack |
| `mlx_vlm_version` | `importlib.metadata.version("mlx-vlm")` | for the 0.6.3 audio-bug note |
| `cuda` | `shutil.which("nvidia-smi") is not None` | coarse NVIDIA GPU presence |
| `libopus` | `app.core.opus.opus_available()` | |
| `ollama` | `shutil.which("ollama")` or `settings.ollama_bin` | LLM runtime |
| `faster_whisper` | `module_available("faster_whisper")` | |
| `vosk` | `module_available("vosk")` | |

## Candidates (grounded in the existing catalogs)

Pulled from the real catalogs; the recommender adds a requirements annotation per
entry. Installed/active state comes from the existing managers' snapshots (no
catalog duplication).

- **STT**: faster-whisper `phowhisper-{small,medium,large}`, `tiny/base/small/medium/large-v3`;
  vosk `*-vn-*` / `*-en-*`; `whisper_mlx` (Apple); `qwen_omni` 4/6/8-bit (Apple);
  remote `whisper_service` / `eventlab` (config, not a download).
- **TTS**: `vieneu v3turbo` (CPU); `vieneu standard/turbo/fast` (NVIDIA GPU);
  `omnivoice k2-fsa/OmniVoice` (CPU/CLI, heavy).
- **LLM**: ollama `gemma2:2b/9b/27b` (CPU-capable); `qwen_omni` audio-native (Apple);
  online OpenAI-compatible (config).
- **VAD**: `energy` (built-in); `silero` (pip); `pyannote` (pip + HF token, gated).

## Item schema (per candidate)

```
{
  "category": "stt|tts|llm|vad",
  "id": "phowhisper-medium",          // key used by the download endpoint
  "engine": "whisper",                // provider/engine family
  "label": "PhoWhisper Medium — Vietnamese",
  "chip": "apple_silicon|cpu|nvidia_gpu",  // hard-requirement partition
  "status": "installed|runnable|needs:<x>|incompatible:<why>",
  "recommended": true,                // runnable on current machine AND fit_score >= 60
  "active": false,                    // currently the active model for its engine family
  "fit_score": 0-100,
  "reason": "Runs on CPU; Vietnamese fine-tune; already installed",
  "size_estimate": "~1.5 GB",
  "action": {                         // how to obtain it
    "kind": "download|pip|config|builtin",
    "method": "POST", "path": "/v1/models/whisper/download",
    "payload": {"size": "phowhisper-medium"},
    "hint": "pip install silero-vad"  // for kind=pip/config
  },
  "select": {                         // activate an installed model (null if unsupported)
    "kind": "select", "method": "POST", "path": "/v1/models/whisper/select",
    "payload": {"size": "phowhisper-medium"}
  }
}
```

UI shows `active` → "active" badge; installed-but-not-active with a `select` → a
"Use" button (POSTs `select`); not-installed → "Download" (POSTs `action`).

### `chip` partition (by hard requirement)
- `apple_silicon`: `whisper_mlx`, `qwen_omni` (STT + LLM)
- `nvidia_gpu`: `vieneu standard/turbo/fast`
- `cpu`: everything else (vosk, faster-whisper, vieneu v3turbo, ollama, omnivoice, all VAD)

### Scoring heuristic (documented, deterministic)
- Hard requirement of `chip` unmet by capabilities → `status=incompatible:<why>`,
  `fit_score = 0` (sorted to the bottom of its category).
- Base `runnable` → +50.
- Quality tier (`high`/`medium`/`low`) → +30 / +15 / +5.
- Vietnamese-capable model → +10 (project is VN-first).
- Already installed → +10.
- Resource gate: `min_ram_gb > ram_total_gb` (when known) → `incompatible:ram`;
  `size_gb > disk_free_gb` (when known) → `needs:disk`. Unknown RAM/disk → no penalty.
- `recommended = runnable and fit_score >= 60`.

## API

`GET /v1/models/recommend`
```
{ "success": true, "data": {
    "capabilities": { ...Capabilities... },
    "categories": { "stt": [...], "tts": [...], "llm": [...], "vad": [...] }
}}
```
Each category list is sorted by `fit_score` desc, then label. One endpoint serves
both UI modes; filtering/grouping happens client-side.

## UI (Models tab panel "Khuyến nghị theo cấu hình")

- A capabilities chip row (e.g. `linux · x86_64 · CPU · 8 GB · no-mlx · no-cuda`).
- Checkbox **"Chỉ hiện khuyến nghị"** (default **checked**): show only items with
  `recommended=true`.
- Unchecked: show **all** candidates, **grouped by `chip`** (Apple Silicon / CPU /
  NVIDIA GPU) with group headers, ranked within each group; items `incompatible`
  with the current machine are dimmed with a reason badge.
- Each row reuses the existing download JS (calls `action.method action.path` with
  `action.payload`); `pip`/`config`/`builtin` rows show the `hint` instead of a
  button.

## File boundaries

- `app/services/recommend/__init__.py`
- `app/services/recommend/capabilities.py` — `Capabilities` + `detect_capabilities()`.
- `app/services/recommend/catalog.py` — `CANDIDATES` annotation table (requirements,
  tier, language, size, chip, action) keyed to real catalog ids.
- `app/services/recommend/recommender.py` — pure `recommend(caps, installed) -> dict`
  (scoring + ranking). `installed` is gathered from existing manager snapshots.
- `app/api/routes/recommend.py` — `GET /v1/models/recommend`; mounted in `main.py`.
- UI: `app/static/index.html` + `app/static/app.js` (new panel + toggle + grouping).

## Error handling

- `detect_capabilities()` never raises; failed probes → `None`.
- The route degrades gracefully: if a manager snapshot fails, that category falls
  back to "not installed" state rather than 500.

## Testing (pytest, asyncio_mode=auto, matches existing suite)

- `recommender` (pure): synthetic `Capabilities` for (a) Linux x86 no-mlx, (b) Apple
  arm64 + mlx, (c) low-RAM — assert ordering, `runnable`, `incompatible`, `chip`
  partition, and `recommended` threshold.
- `capabilities`: parsing with mocked `platform`/`/proc/meminfo` (Linux) and a
  no-mlx environment; assert no exceptions and sane fields.
- route: `TestClient` GET `/v1/models/recommend` → 200, shape, each category sorted.
