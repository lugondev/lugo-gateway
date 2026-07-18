"""The candidate catalog — every downloadable/selectable model annotated with the
requirements the recommender scores against. IDs match what each manager's download
endpoint expects and what its snapshot reports as installed.
"""

from app.services.recommend.recommender import Candidate


def _dl(method_path: str, **payload) -> dict:
    return {"kind": "download", "method": "POST", "path": method_path, "payload": payload}


def _config(hint: str) -> dict:
    return {"kind": "config", "hint": hint}


def _pip(hint: str) -> dict:
    return {"kind": "pip", "hint": hint}


_WHISPER = "/v1/models/whisper/download"
_VOSK = "/v1/models/vosk/download"
_OMNI = "/v1/models/omnivoice/download"
_VIENEU = "/v1/models/vieneu/download"
_LLM = "/v1/models/llm/download"


CANDIDATES: list[Candidate] = [
    # ---- STT: faster-whisper (standard sizes), CPU ----
    Candidate("stt", "large-v3", "whisper", "Whisper Large v3 (multilingual)",
              "cpu", "high", False, 3.0, "~3 GB", 6.0, ["faster_whisper"],
              _dl(_WHISPER, size="large-v3")),
    Candidate("stt", "medium", "whisper", "Whisper Medium (multilingual)",
              "cpu", "medium", False, 1.5, "~1.5 GB", 4.0, ["faster_whisper"],
              _dl(_WHISPER, size="medium")),
    Candidate("stt", "small", "whisper", "Whisper Small (multilingual)",
              "cpu", "medium", False, 0.5, "~0.5 GB", 2.0, ["faster_whisper"],
              _dl(_WHISPER, size="small")),
    Candidate("stt", "base", "whisper", "Whisper Base (multilingual)",
              "cpu", "low", False, 0.15, "~150 MB", 1.0, ["faster_whisper"],
              _dl(_WHISPER, size="base")),
    Candidate("stt", "tiny", "whisper", "Whisper Tiny (fastest, multilingual)",
              "cpu", "low", False, 0.075, "~75 MB", 1.0, ["faster_whisper"],
              _dl(_WHISPER, size="tiny")),
    # ---- STT: vosk (streaming, CPU) ----
    Candidate("stt", "vosk-model-vn-0.4", "vosk", "Vosk Vietnamese (large, streaming)",
              "cpu", "medium", True, 1.6, "~1.6 GB", 2.0, ["vosk"],
              _dl(_VOSK, name="vosk-model-vn-0.4")),
    Candidate("stt", "vosk-model-small-vn-0.4", "vosk", "Vosk Vietnamese (small, streaming)",
              "cpu", "medium", True, 0.05, "~50 MB", 1.0, ["vosk"],
              _dl(_VOSK, name="vosk-model-small-vn-0.4")),
    Candidate("stt", "vosk-model-en-us-0.22", "vosk", "Vosk English (large, streaming)",
              "cpu", "medium", False, 1.8, "~1.8 GB", 2.0, ["vosk"],
              _dl(_VOSK, name="vosk-model-en-us-0.22")),
    Candidate("stt", "vosk-model-small-en-us-0.15", "vosk", "Vosk English (small, streaming)",
              "cpu", "low", False, 0.07, "~70 MB", 1.0, ["vosk"],
              _dl(_VOSK, name="vosk-model-small-en-us-0.15")),
    # ---- STT: Apple Silicon (MLX) ----
    Candidate("stt", "whisper-large-v3-turbo-mlx", "whisper_mlx",
              "Whisper Large v3 Turbo — MLX (Apple GPU, ~7×)",
              "apple_silicon", "high", False, 1.5, "~1.5 GB", 4.0, ["mlx"],
              _config("bash scripts/convert_phowhisper_mlx.sh (Apple Silicon only)")),
    # ---- STT: Qwen3-ASR — multilingual incl. Vietnamese (Apple MLX or NVIDIA CUDA) ----
    Candidate("stt", "qwen3-asr-mlx", "qwen3_asr",
              "Qwen3-ASR — Apple GPU (MLX), multilingual incl. Vietnamese ⭐",
              "apple_silicon", "high", True, 1.2, "~1.2 GB", 8.0, ["mlx_qwen3_asr"],
              _pip("pip install mlx-qwen3-asr  (optional `qwen3-asr` extra; Apple Silicon only)")),
    Candidate("stt", "qwen3-asr-cuda", "qwen3_asr",
              "Qwen3-ASR — NVIDIA GPU (CUDA), multilingual incl. Vietnamese ⭐",
              "nvidia_gpu", "high", True, 1.2, "~1.2 GB", None, ["qwen_asr"],
              _pip("pip install qwen-asr  (optional `qwen3-asr-cuda` extra; needs an NVIDIA GPU)")),
    # ---- STT: remote (config, no download) ----
    Candidate("stt", "whisper_service", "whisper_service", "Remote Whisper (OpenAI-compatible)",
              "cpu", "medium", False, None, "remote API", None, ["whisper_service"],
              _config("Set WHISPER_SERVICE_BASE_URL + WHISPER_SERVICE_API_KEY")),
    Candidate("stt", "eventlab", "eventlab", "Eventlab STT (remote)",
              "cpu", "medium", False, None, "remote API", None, ["eventlab"],
              _config("Set EVENTLAB_BASE_URL + EVENTLAB_API_KEY")),
    Candidate("stt", "qwen3_asr_or", "qwen3_asr_or", "Qwen3-ASR Flash via OpenRouter (remote)",
              "cpu", "medium", True, None, "remote API", None, ["openrouter"],
              _config("Set the OpenRouter API key in system config")),
    Candidate("stt", "whisper_or", "whisper_or", "Whisper Large v3 Turbo via OpenRouter (remote)",
              "cpu", "medium", False, None, "remote API", None, ["openrouter"],
              _config("Set the OpenRouter API key in system config")),

    # ---- TTS ----
    Candidate("tts", "v3turbo", "vieneu", "VieNeu-TTS v3 Turbo — Vietnamese (48kHz, CPU)",
              "cpu", "high", True, 0.5, "~0.5 GB", 2.0, ["vieneu"],
              _dl(_VIENEU, mode="v3turbo")),
    Candidate("tts", "k2-fsa/OmniVoice", "omnivoice", "OmniVoice (multilingual, 24kHz)",
              "cpu", "medium", True, 6.5, "~6.5 GB", 4.0, ["omnivoice"],
              _dl(_OMNI, id="k2-fsa/OmniVoice")),
    Candidate("tts", "standard", "vieneu", "VieNeu-TTS Standard (GPU)",
              "nvidia_gpu", "high", True, 0.5, "~0.5 GB", None, ["vieneu"],
              _dl(_VIENEU, mode="standard")),
    Candidate("tts", "turbo", "vieneu", "VieNeu-TTS Turbo (GPU)",
              "nvidia_gpu", "high", True, 0.5, "~0.5 GB", None, ["vieneu"],
              _dl(_VIENEU, mode="turbo")),
    Candidate("tts", "fast", "vieneu", "VieNeu-TTS Fast (GPU)",
              "nvidia_gpu", "high", True, 0.5, "~0.5 GB", None, ["vieneu"],
              _dl(_VIENEU, mode="fast")),

    # ---- LLM ----
    Candidate("llm", "gemma2:9b", "ollama", "Gemma 2 9B (balanced)",
              "cpu", "medium", False, 5.4, "~5.4 GB", 8.0, ["ollama"],
              _dl(_LLM, model="gemma2:9b")),
    Candidate("llm", "gemma2:2b", "ollama", "Gemma 2 2B (fast)",
              "cpu", "low", False, 1.6, "~1.6 GB", 4.0, ["ollama"],
              _dl(_LLM, model="gemma2:2b")),
    Candidate("llm", "gemma2:27b", "ollama", "Gemma 2 27B (best quality)",
              "cpu", "high", False, 16.0, "~16 GB", 32.0, ["ollama"],
              _dl(_LLM, model="gemma2:27b")),
    Candidate("llm", "online", "online", "Online LLM (OpenAI-compatible API)",
              "cpu", "high", False, None, "remote API", None, ["online_llm"],
              _config("Set CONVERSATION_LLM_BASE_URL + CONVERSATION_LLM_API_KEY")),

    # ---- VAD ----
    Candidate("vad", "energy", "energy", "Energy gate (built-in)",
              "cpu", "medium", False, None, "built-in", None, [],
              _config("Built-in — no install needed")),
    Candidate("vad", "silero", "silero", "Silero VAD (accurate, CPU)",
              "cpu", "high", False, 0.05, "~50 MB", None, ["silero_vad"],
              _pip("pip install silero-vad")),
    Candidate("vad", "pyannote", "pyannote", "pyannote segmentation (gated)",
              "cpu", "high", False, 0.05, "~50 MB", None, ["pyannote.audio"],
              _pip("pip install pyannote.audio + set PYANNOTE_AUTH_TOKEN (gated)")),
]

# Engines whose active model can be switched via a /select endpoint. The select
# payload mirrors the download payload (same key/value), so reuse it.
_SELECT_PATHS = {
    "whisper": "/v1/models/whisper/select",
    "vosk": "/v1/models/vosk/select",
    "omnivoice": "/v1/models/omnivoice/select",
    "vieneu": "/v1/models/vieneu/select",
    "ollama": "/v1/models/llm/select",
}

for _c in CANDIDATES:
    _path = _SELECT_PATHS.get(_c.engine)
    if _path and _c.action.get("kind") == "download":
        _c.select = {"kind": "select", "method": "POST", "path": _path, "payload": _c.action["payload"]}
