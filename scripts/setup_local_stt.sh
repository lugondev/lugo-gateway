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
from app.core.settings import settings

model = WhisperModel(
    settings.whisper_local_model,
    device=settings.whisper_local_device,
    compute_type=settings.whisper_local_compute_type,
)
print("Whisper local model is ready:", settings.whisper_local_model)
_ = model
PY

echo "Local STT setup completed."
