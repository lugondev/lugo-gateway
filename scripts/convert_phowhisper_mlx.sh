#!/usr/bin/env bash
# Convert a Whisper model to MLX for the Apple-Silicon GPU STT engine.
#
# CTranslate2 (faster-whisper) is CPU-only on macOS; MLX runs on the Metal GPU and
# is ~7x faster on M-series. This converts the HF transformers weights to MLX
# float16 into WHISPER_MLX_MODEL_PATH so the `whisper_mlx` STT engine can load it.
#
# Requires: mlx-whisper (pip install mlx-whisper) on Apple Silicon.
# Usage:   scripts/convert_phowhisper_mlx.sh [HF_MODEL] [OUT_DIR]
set -euo pipefail

HF_MODEL="${1:-openai/whisper-large-v3-turbo}"
OUT_DIR="${2:-models/stt/whisper-large-v3-turbo-mlx}"
PY="${PYTHON:-.venv/bin/python}"

if ! "$PY" -c "import mlx_whisper" 2>/dev/null; then
  echo "mlx-whisper not installed. Run: $PY -m pip install mlx-whisper (Apple Silicon only)"
  exit 1
fi

if [ -d "$OUT_DIR" ] && [ -f "$OUT_DIR/model.safetensors" ]; then
  echo "Already converted: $OUT_DIR"
  exit 0
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "Fetching mlx-examples whisper converter…"
curl -fsSL -o "$TMP/convert.py" \
  "https://raw.githubusercontent.com/ml-explore/mlx-examples/main/whisper/convert.py"

echo "Converting $HF_MODEL -> $OUT_DIR (float16)…"
"$PY" "$TMP/convert.py" --torch-name-or-path "$HF_MODEL" --mlx-path "$OUT_DIR" --dtype float16

# The pip mlx-whisper loader expects weights.safetensors; the converter writes
# model.safetensors. Rename so load_model() finds it.
if [ -f "$OUT_DIR/model.safetensors" ] && [ ! -f "$OUT_DIR/weights.safetensors" ]; then
  mv "$OUT_DIR/model.safetensors" "$OUT_DIR/weights.safetensors"
fi
echo "Done: $OUT_DIR"
