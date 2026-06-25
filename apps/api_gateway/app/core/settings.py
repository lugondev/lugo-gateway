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
    pyannote_vad_model: str = "pyannote/voice-activity-detection"
    pyannote_auth_token: str = ""

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
