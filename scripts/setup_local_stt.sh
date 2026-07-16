#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi

source .venv/bin/activate

pip install --upgrade pip
pip install -e .

"$ROOT_DIR/scripts/download_vosk_model.sh"

echo "Warming up faster-whisper model (this downloads model weights)..."
PYTHONPATH=apps/api_gateway python - <<'PY'
from faster_whisper import WhisperModel
from app.services.model_registry.resolve import (
    resolve_stt_engine_config,
    resolve_stt_local_device,
)
from app.services.stt.providers.whisper_provider import resolve_whisper_model

model_name = resolve_stt_engine_config("whisper_local")["default_model"]
device_cfg = resolve_stt_local_device("whisper_local")
model = WhisperModel(
    resolve_whisper_model(model_name),
    device=device_cfg["device"] or "cpu",
    compute_type=device_cfg["compute_type"],
)
print("Whisper local model is ready:", model_name)
_ = model
PY

echo "Local STT setup completed."
