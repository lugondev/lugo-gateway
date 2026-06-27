from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "speech-text-transformer"
    app_env: str = "dev"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"

    cors_allow_origins: str = "*"

    default_stt_engine: str = "vosk"
    default_tts_engine: str = "omnivoice"

    omnivoice_path: str = "/Users/lugon/code/OmniVoice"
    omnivoice_model_id: str = "k2-fsa/OmniVoice"
    omnivoice_device: str = ""  # empty = auto-detect (cuda/mps/cpu)
    omnivoice_dtype: str = "float16"
    # Python interpreter that can import omnivoice (its own venv). Empty = auto.
    omnivoice_python: str = ""
    omnivoice_timeout_seconds: float = 600.0
    # Persistent inference server (loads the model once) -> real-time-ish TTS.
    # Falsey -> fall back to the per-call CLI (reloads the model every call).
    omnivoice_use_server: bool = True
    omnivoice_server_host: str = "127.0.0.1"
    omnivoice_server_port: int = 8762
    omnivoice_server_startup_seconds: float = 60.0
    # Pin a consistent voice: auto mode picks a RANDOM voice per call (different
    # voice per sentence/chunk). A fixed instruct + greedy sampling keeps one voice.
    # Must use OmniVoice voice-design attributes (gender/age/pitch/accent/style),
    # comma+space separated, e.g. "female, young adult" or "male, low pitch".
    omnivoice_default_instruct: str = "female, young adult"
    omnivoice_class_temperature: float = 0.0  # 0 = deterministic (consistent voice)
    # A fixed reference voice is generated once (from the instruct above) and then
    # CLONED for every chunk, so all sentences use exactly the same voice.
    omnivoice_pin_voice: bool = True
    omnivoice_ref_text: str = "Xin chào, đây là giọng đọc tham chiếu để giữ giọng nhất quán."

    default_tts_engine_voice: str = ""  # optional VieNeu preset voice
    enable_mock_engines: bool = True

    artifacts_dir: str = "artifacts"

    stt_model_dir: str = "models/stt"
    vosk_model_path: str = "models/stt/vosk-model-small-en-us-0.15"
    vosk_model_base_url: str = "https://alphacephei.com/vosk/models"
    stt_stream_sample_rate: int = 16000

    whisper_local_model: str = "small"
    whisper_local_device: str = "cpu"
    whisper_local_compute_type: str = "int8"

    # Audio preprocessing for STT (defaults; overridable per request)
    stt_vad_enabled: bool = True
    stt_vad_backend: str = "energy"  # energy | silero | pyannote
    stt_noise_reduce_enabled: bool = False
    stt_noise_reduce_amount: float = 0.85

    # Pyannote VAD model + optional Hugging Face token (gated models)
    pyannote_vad_model: str = "pyannote/segmentation-3.0"
    pyannote_auth_token: str = ""

    # whisper_gemma: faster-whisper transcript refined by the conversation LLM
    stt_enhance_timeout_seconds: float = 30.0
    stt_enhance_prompt: str = (
        "You are an ASR post-editor. Fix spelling, casing, punctuation and obvious "
        "speech-recognition errors in the transcript. Do NOT translate, do NOT answer it, "
        "do NOT add or remove meaning. Return ONLY the corrected transcript text."
    )

    # Conversation (voice turn-taking) defaults
    conversation_silence_ms: int = 500       # trailing silence that ends a turn (snappier turn-taking)
    conversation_min_speech_ms: int = 300    # ignore utterances shorter than this
    conversation_rms_threshold: float = 0.015  # speech vs silence (float RMS)
    conversation_max_utterance_ms: int = 30000
    conversation_stt_engine: str = "whisper"  # better than vosk for Vietnamese
    conversation_tts_engine: str = "vieneu"  # in-process & warm (~0.4s); OmniVoice CLI reloads per call (~7s)
    conversation_language: str = "vi"        # STT language hint; "" = auto-detect
    # Optional OpenAI-compatible chat endpoint (Ollama/LM Studio/vLLM/OpenAI).
    # Empty base url -> built-in echo responder (no external service).
    conversation_llm_base_url: str = ""
    conversation_llm_api_key: str = ""
    conversation_llm_model: str = "gpt-3.5-turbo"
    ollama_bin: str = ""  # path to the ollama binary; empty = auto-detect
    conversation_llm_timeout_seconds: float = 60.0
    conversation_system_prompt: str = (
        "You are a helpful, concise voice assistant. Reply in the user's language, "
        "in 2-4 short sentences suitable for being spoken aloud."
    )

    whisper_service_base_url: str = ""
    whisper_service_api_key: str = ""
    whisper_service_model: str = "whisper-1"

    eventlab_base_url: str = ""
    eventlab_api_key: str = ""
    eventlab_model: str = "whisper-1"
    remote_stt_timeout_seconds: float = 60.0

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def omnivoice_python_path(self) -> str:
        return self.omnivoice_python or f"{self.omnivoice_path.rstrip('/')}/.venv/bin/python"

    @property
    def cors_origins_list(self) -> list[str]:
        value = self.cors_allow_origins.strip()
        if not value or value == "*":
            return ["*"]
        return [origin.strip() for origin in value.split(",") if origin.strip()]


settings = Settings()
