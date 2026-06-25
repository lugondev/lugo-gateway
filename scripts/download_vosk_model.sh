#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MODEL_DIR="${STT_MODEL_DIR:-$ROOT_DIR/models/stt}"
MODEL_NAME="${VOSK_MODEL_NAME:-vosk-model-small-en-us-0.15}"
MODEL_URL="${VOSK_MODEL_URL:-https://alphacephei.com/vosk/models/${MODEL_NAME}.zip}"
ZIP_PATH="$MODEL_DIR/${MODEL_NAME}.zip"

mkdir -p "$MODEL_DIR"

if [[ -d "$MODEL_DIR/$MODEL_NAME" ]]; then
  echo "Vosk model already exists at $MODEL_DIR/$MODEL_NAME"
  exit 0
fi

echo "Downloading Vosk model: $MODEL_URL"
curl -L "$MODEL_URL" -o "$ZIP_PATH"

echo "Extracting model to $MODEL_DIR"
unzip -q "$ZIP_PATH" -d "$MODEL_DIR"
rm -f "$ZIP_PATH"

echo "Done. Model path: $MODEL_DIR/$MODEL_NAME"
