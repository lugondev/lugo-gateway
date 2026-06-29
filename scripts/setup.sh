#!/usr/bin/env bash
# Setup wizard — detect the host (Apple Silicon / NVIDIA GPU / CPU) and install the
# matching engine packages + system libs. Pick extras with flags ("tick" what you want).
#
#   bash scripts/setup.sh                      # host-appropriate STT + TTS
#   bash scripts/setup.sh --gpu-tts            # + VieNeu GPU modes (NVIDIA)
#   bash scripts/setup.sh --ollama gemma2:2b   # + local LLM via Ollama
#   bash scripts/setup.sh --dry-run            # print the plan, install nothing
set -euo pipefail

GPU_TTS=0
OLLAMA=""
DRY=0
LIST=0
PY="${PYTHON:-python}"   # install into THIS interpreter (use system python on Colab)

usage() {
  cat <<'EOF'
Setup wizard for speech-text-transformer.

Options (tick what you want):
  --gpu-tts          VieNeu GPU modes (vieneu[gpu]: llama-cpp-python + lmdeploy) — NVIDIA only
  --ollama [MODEL]   Install Ollama + pull MODEL (default gemma2:2b) for a local conversation LLM
  --dry-run          Print the plan only; install nothing
  -h, --help         Show this help

Engines installed per detected host:
  Apple Silicon -> .[mlx,qwen3-asr,tts,opus]  (whisper_mlx, qwen3_asr MLX, VieNeu, Opus)
  NVIDIA GPU    -> .[qwen3-asr-cuda,tts]       (qwen3_asr CUDA, VieNeu)  [+ vieneu[gpu] with --gpu-tts]
  CPU only      -> .[tts,opus]                 (whisper/PhoWhisper CPU, VieNeu v3turbo, Opus)
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --gpu-tts) GPU_TTS=1; shift ;;
    --ollama) shift; if [ $# -gt 0 ] && [[ "$1" != -* ]]; then OLLAMA="$1"; shift; else OLLAMA="gemma2:2b"; fi ;;
    --dry-run) DRY=1; shift ;;
    --list) LIST=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

run() { echo "+ $*"; [ "$DRY" -eq 1 ] || "$@"; }
sh_run() { echo "+ $*"; [ "$DRY" -eq 1 ] || bash -c "$*"; }

# ---- detect host ----
OS=$(uname -s); ARCH=$(uname -m)
if [ "$OS" = "Darwin" ] && { [ "$ARCH" = "arm64" ] || [ "$ARCH" = "aarch64" ]; }; then
  HOST=apple
elif command -v nvidia-smi >/dev/null 2>&1 || [ -e /proc/driver/nvidia/version ]; then
  HOST=nvidia
else
  HOST=cpu
fi
echo "==> Host: $HOST ($OS/$ARCH) — python: $($PY --version 2>&1)"

# ---- host-filtered menu: only what can run here ----
print_menu() {
  echo
  echo "Installable on this host ($HOST):"
  case "$HOST" in
    apple)
      echo "  STT : whisper_mlx, qwen3_asr (MLX), whisper/PhoWhisper (CPU), vosk"
      echo "  TTS : VieNeu v3turbo (CPU)"
      echo "  LLM : Ollama (--ollama MODEL)  |  online (UI)"
      echo "  Skipped (incompatible): qwen3_asr CUDA, VieNeu GPU modes  (need NVIDIA)" ;;
    nvidia)
      echo "  STT : qwen3_asr (CUDA) ⭐, whisper/PhoWhisper (CPU), vosk"
      echo "  TTS : VieNeu v3turbo (CPU)  |  VieNeu GPU modes (--gpu-tts)"
      echo "  LLM : Ollama (--ollama MODEL)  |  online (UI)"
      echo "  Skipped (incompatible): whisper_mlx, qwen3_asr MLX, qwen_omni  (Apple Silicon only)" ;;
    cpu)
      echo "  STT : whisper/PhoWhisper (CPU), vosk"
      echo "  TTS : VieNeu v3turbo (CPU)"
      echo "  LLM : online (UI)  |  Ollama (--ollama MODEL, slow on CPU)"
      echo "  Skipped (incompatible): qwen3_asr (needs GPU), whisper_mlx/qwen_omni (Apple), VieNeu GPU modes" ;;
  esac
  echo
}
print_menu
[ "$LIST" -eq 1 ] && exit 0

# ---- system libraries ----
if [ "$OS" = "Linux" ] && command -v apt-get >/dev/null 2>&1; then
  SUDO=""; command -v sudo >/dev/null 2>&1 && [ "$(id -u)" -ne 0 ] && SUDO="sudo"
  PKGS="libsndfile1 libopus0"            # TTS (soundfile) + Opus transport
  [ -n "$OLLAMA" ] && PKGS="$PKGS zstd"  # Ollama installer needs zstd
  sh_run "$SUDO apt-get -qq update"
  sh_run "$SUDO apt-get -qq install -y $PKGS"
elif [ "$HOST" = "apple" ]; then
  command -v brew >/dev/null 2>&1 && run brew install opus libsndfile || echo "  (brew not found — install opus/libsndfile manually if needed)"
fi

# ---- python extras per host ----
case "$HOST" in
  apple)  EXTRAS="mlx,qwen3-asr,tts,opus" ;;
  nvidia) EXTRAS="qwen3-asr-cuda,tts" ;;
  cpu)    EXTRAS="tts,opus" ;;
esac
run "$PY" -m pip install -e ".[$EXTRAS]"

if [ "$GPU_TTS" -eq 1 ]; then
  if [ "$HOST" = "nvidia" ]; then
    sh_run "$PY -m pip install 'vieneu[gpu]'"   # llama-cpp-python + lmdeploy (Turbo/Fast/Standard modes)
  else
    echo "  (skip --gpu-tts: no NVIDIA GPU detected)"
  fi
fi

# ---- optional local LLM via Ollama ----
if [ -n "$OLLAMA" ]; then
  if ! command -v ollama >/dev/null 2>&1; then
    sh_run "curl -fsSL https://ollama.com/install.sh | sh"
  fi
  sh_run "nohup ollama serve >/tmp/ollama.log 2>&1 &"
  run sleep 5
  run ollama pull "$OLLAMA"
fi

# ---- persist runtime config to .env (gateway reads it; no manual env vars) ----
upsert_env() {  # upsert_env KEY VALUE
  local k="$1" v="$2" f=".env"
  echo "  .env: $k=$v"
  [ "$DRY" -eq 1 ] && return 0
  touch "$f"
  if grep -qE "^${k}=" "$f"; then
    sed -i.bak "s|^${k}=.*|${k}=${v}|" "$f" && rm -f "$f.bak"
  else
    echo "${k}=${v}" >> "$f"
  fi
}

echo
echo "==> Writing runtime config to .env:"
upsert_env ENABLE_MOCK_ENGINES false
upsert_env ALLOW_RUNTIME_INSTALL true
upsert_env DEFAULT_TTS_ENGINE vieneu
upsert_env CONVERSATION_TTS_ENGINE vieneu
upsert_env CONVERSATION_AUDIO_NATIVE false
upsert_env CONVERSATION_STT_ENGINE "$([ "$HOST" = cpu ] && echo whisper || echo qwen3_asr)"
if [ -n "$OLLAMA" ]; then
  upsert_env CONVERSATION_LLM_BASE_URL http://localhost:11434/v1
  upsert_env CONVERSATION_LLM_MODEL "$OLLAMA"
fi

cat <<'EOF'

==> Done. The gateway reads .env — just run (no env vars needed):
    PYTHONPATH=apps/api_gateway python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
EOF
