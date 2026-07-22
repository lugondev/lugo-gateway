#!/usr/bin/env bash
# Build the qwen3-asr.cpp CLI and fetch a GGUF model for the `qwen3_asr_gguf` STT
# engine — Qwen3-ASR on CPU (and Apple Metal), no Python runtime for inference.
#
# qwen3-asr.cpp (https://github.com/predict-woo/qwen3-asr.cpp) is pure C++17/GGML.
# On the CPU-only Linux deploy this is the only Qwen3-ASR path (the mlx/qwen-asr
# Python backends are GPU-only and auto-hide). On Apple Silicon it builds with
# Metal for GPU acceleration.
#
# Requires: git, cmake (>=3.14), make, a C++17 compiler. On Debian slim:
#           apt-get install -y git cmake make g++
# Usage:    bash apps/model_service/scripts/build_qwen3_asr_cpp.sh [GGUF_URL]
#
# Outputs (both git-ignored via build/ and *.gguf):
#   apps/model_service/vendor/qwen3-asr.cpp/build/qwen3-asr-cli   (the binary)
#   apps/model_service/vendor/qwen3-asr.cpp/models/*.gguf         (the weights)
# These match qwen3_asr_gguf_provider.py's default binary_path / default_model.
set -euo pipefail

REPO_URL="https://github.com/predict-woo/qwen3-asr.cpp.git"
# predict-woo/qwen3-asr.cpp uses its OWN GGUF layout (converted via its bundled
# scripts/convert_hf_to_gguf.py). The prebuilt ggml-org/*-GGUF files are a DIFFERENT
# (llama.cpp) layout and will NOT load here — so we convert from the original Alibaba
# safetensors instead of downloading a GGUF. HF_MODEL is the source repo; QUANT the
# output precision (q8_0 ~1.3GB is the CPU-friendly default; f16 ~1.8GB).
HF_MODEL="${1:-Qwen/Qwen3-ASR-1.7B}"
QUANT="${2:-q8_0}"

# Resolve repo root from this script's location so it works from any cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENDOR_DIR="$SCRIPT_DIR/../vendor/qwen3-asr.cpp"
BUILD_BIN="$VENDOR_DIR/build/qwen3-asr-cli"
MODEL_DIR="$VENDOR_DIR/models"
# Derive the output filename from the HF repo (e.g. Qwen/Qwen3-ASR-1.7B ->
# qwen3-asr-1.7b) so switching HF_MODEL doesn't collide with a previously
# converted file of a different size. Lowercased basename + quant.
MODEL_SLUG="$(basename "$HF_MODEL" | tr '[:upper:]' '[:lower:]')"
MODEL_FILE="$MODEL_DIR/${MODEL_SLUG}-${QUANT}.gguf"

# Python used for the HF->GGUF conversion. Prefer the repo venv (has torch/
# transformers); fall back to python3. `gguf` is installed on demand below.
PY="${PYTHON:-$SCRIPT_DIR/../../../.venv/bin/python}"
[ -x "$PY" ] || PY="python3"

for tool in git cmake make; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "Missing required tool: $tool" >&2
    exit 1
  fi
done

# 1. Clone (with the bundled GGML submodule) or update.
if [ ! -d "$VENDOR_DIR/.git" ]; then
  echo "Cloning qwen3-asr.cpp -> $VENDOR_DIR"
  git clone --recursive "$REPO_URL" "$VENDOR_DIR"
else
  echo "Updating existing checkout: $VENDOR_DIR"
  git -C "$VENDOR_DIR" pull --ff-only
  git -C "$VENDOR_DIR" submodule update --init --recursive
fi

# 2. Build (Release). Reuses an existing binary unless the source changed.
if [ -x "$BUILD_BIN" ]; then
  echo "Binary already built: $BUILD_BIN (delete build/ to force a rebuild)"
else
  echo "Building qwen3-asr-cli (Release)…"
  if command -v nproc >/dev/null 2>&1; then
    JOBS="$(nproc)"
  else
    JOBS="$(sysctl -n hw.ncpu 2>/dev/null || echo 4)"
  fi
  cmake -S "$VENDOR_DIR" -B "$VENDOR_DIR/build" -DCMAKE_BUILD_TYPE=Release
  cmake --build "$VENDOR_DIR/build" -j "$JOBS"
fi

# 3. Produce the GGUF weights by converting the HF safetensors with the fork's own
#    converter (there is no compatible prebuilt GGUF to download).
mkdir -p "$MODEL_DIR"
if [ -f "$MODEL_FILE" ]; then
  echo "Model already present: $MODEL_FILE"
else
  echo "Converting $HF_MODEL -> $MODEL_FILE ($QUANT) using $PY"
  # The converter needs torch/safetensors/tqdm/gguf. Present already in the repo
  # venv (other engines pull in torch), but NOT in the model_service Docker image
  # (that image's [tts,opus] extras have no ML training deps) -- install on
  # demand there. CPU-only torch wheel to avoid a multi-GB CUDA pull.
  "$PY" -c "import torch" 2>/dev/null \
    || "$PY" -m pip install -q torch --index-url https://download.pytorch.org/whl/cpu
  "$PY" -c "import safetensors, tqdm" 2>/dev/null || "$PY" -m pip install -q safetensors tqdm
  "$PY" -c "import gguf" 2>/dev/null || "$PY" -m pip install -q gguf
  "$PY" -c "import huggingface_hub" 2>/dev/null || "$PY" -m pip install -q huggingface_hub
  # download_hf_snapshot: reuse the HF cache if already present (the mlx/qwen-asr
  # engines share it), else huggingface_hub pulls it. -m gguf resolves the snapshot.
  SNAP="$("$PY" - "$HF_MODEL" <<'PYEOF'
import sys
from huggingface_hub import snapshot_download
print(snapshot_download(sys.argv[1]))
PYEOF
)"
  echo "HF snapshot: $SNAP"
  "$PY" "$VENDOR_DIR/scripts/convert_hf_to_gguf.py" \
      --input "$SNAP" --output "$MODEL_FILE" --type "$QUANT"
fi

echo ""
echo "Done."
echo "  binary: $BUILD_BIN"
echo "  model:  $MODEL_FILE"
echo ""
echo "Smoke test:  $BUILD_BIN -m $MODEL_FILE -f your_audio_16k_mono.wav --language vi"
