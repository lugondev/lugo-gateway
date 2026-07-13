from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "speech-text-transformer"
    app_env: str = "dev"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"

    cors_allow_origins: str = "*"

    # Browser control-panel login (single shared password). Empty = auth disabled.
    admin_password: str = ""
    # Cookie-signing secret for the login session. Empty (with admin_password set)
    # -> a random secret is generated at process startup (sessions reset on restart).
    session_secret: str = ""
    # Shared secret for ESP32/RPi device WS clients, which can't do a browser
    # cookie login. Empty = device WS connections are rejected while
    # admin_password is set (browsers still work via cookie session).
    # NOTE: sent as a WS URL query param (not a header), so it will appear in
    # plaintext in standard reverse-proxy/uvicorn access logs -- account for
    # that in log handling/retention if you enable this.
    device_auth_token: str = ""

    # Bootstrap admin account, created once on startup if the `users` table is
    # empty. Falls back to admin_password (legacy single-secret login) with
    # username "admin" if these are unset, so upgrading an existing deployment
    # doesn't lock the operator out.
    admin_bootstrap_username: str = ""
    admin_bootstrap_password: str = ""

    omnivoice_path: str = "/Users/lugon/code/OmniVoice"
    omnivoice_model_id: str = "k2-fsa/OmniVoice"
    omnivoice_device: str = ""  # empty = auto-detect (cuda/mps/cpu)
    omnivoice_dtype: str = "float16"
    # Python interpreter that can import omnivoice (its own venv). Empty = auto.
    omnivoice_python: str = ""
    # Real-time conversation TTS: a single /synth call normally finishes in a
    # few seconds. A much longer cap (e.g. minutes) makes a stalled sidecar
    # request indistinguishable from a permanent hang to whoever's talking.
    omnivoice_timeout_seconds: float = 45.0
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

    # Allow the /v1/models/install endpoint to pip-install engine packages at runtime.
    # OFF by default — keep it OFF on public deploys (it runs pip on the server). Turn
    # ON for local/Colab convenience. Installs are restricted to a fixed allowlist.
    allow_runtime_install: bool = False

    artifacts_dir: str = "artifacts"

    stt_model_dir: str = "models/stt"
    vosk_model_path: str = "models/stt/vosk-model-small-en-us-0.15"
    vosk_model_base_url: str = "https://alphacephei.com/vosk/models"
    stt_stream_sample_rate: int = 16000

    # PhoWhisper (VinAI) — Whisper fine-tuned on 844h Vietnamese. Far better tones/
    # diacritics than vanilla Whisper. CT2 build runs in faster-whisper at the same
    # speed class as the equivalent vanilla size. Standard sizes ("small"/"medium"/
    # "large-v3") still work; PhoWhisper ids: "phowhisper-{tiny,base,small,medium,large}".
    whisper_local_model: str = "phowhisper-medium"
    whisper_local_device: str = "cpu"
    whisper_local_compute_type: str = "int8"
    # Whisper's OWN (Silero) VAD — keep on; it removes silence well and speeds up.
    whisper_vad_filter: bool = True
    # Decoding quality knobs (apply to all whisper-family engines). beam_size=1
    # (greedy) is ~17% faster than 5 with no measured accuracy loss on PhoWhisper —
    # favors conversation latency. Raise to 5 for best batch-transcription quality.
    whisper_beam_size: int = 1
    # Off: avoids hallucination/repetition drift across silent gaps (important for
    # short conversation turns). Initial prompt seeds Vietnamese orthography; empty = off.
    whisper_condition_on_previous_text: bool = False
    whisper_initial_prompt: str = ""
    # Optional hotword/glossary file (one domain term per line, "#" comments). Merged
    # into the Whisper initial prompt to bias recognition toward domain vocabulary
    # (product names, wake-words, commands) — the Whisper-family analogue of a
    # FunASR hotword list. Empty = no glossary biasing.
    stt_glossary_path: str = ""
    # Language preset -> (engine, language). One of: vi | en | multi | en_vi (see
    # services/stt/profile.py). Empty = use conversation_stt_engine/language as-is.
    # An explicit stt_engine/language (query param) still overrides the profile.
    stt_profile: str = ""

    # whisper_mlx: Apple Silicon GPU path (mlx-whisper). ~7x faster than CPU faster-
    # whisper on M-series. Point at a locally converted MLX model dir (see
    # scripts/convert_phowhisper_mlx.sh). Engine auto-hides when mlx_whisper is absent
    # (non-Mac) or the dir is missing -> callers fall back to the faster-whisper engine.
    whisper_mlx_model_path: str = "models/stt/phowhisper-medium-mlx"

    # Qwen3-ASR (engine "qwen3_asr"), multilingual incl. Vietnamese. Two GPU backends,
    # auto-selected: mlx-qwen3-asr (Apple, `qwen3-asr` extra) or qwen-asr (NVIDIA/CUDA,
    # `qwen3-asr-cuda` extra). 0.6B (default, light, verified VN) or Qwen/Qwen3-ASR-1.7B
    # (higher accuracy). qwen3_asr_device applies to the CUDA backend (empty = cuda:0).
    qwen3_asr_model: str = "Qwen/Qwen3-ASR-0.6B"
    qwen3_asr_device: str = ""

    # Extra STT preprocessing (defaults OFF: our energy gate / spectral denoise can
    # clip or add artifacts and don't help Whisper, which has its own VAD).
    stt_vad_enabled: bool = False
    stt_vad_backend: str = "energy"  # energy | silero | pyannote
    stt_noise_reduce_enabled: bool = False
    stt_noise_reduce_amount: float = 0.85

    # VAD-segmented parallel transcription for long batch audio (FunASR-style): split
    # on silence, transcribe segments concurrently, merge. Off by default; when on it
    # only kicks in for clips at/over stt_segment_min_seconds.
    stt_segment_long_enabled: bool = False
    stt_segment_min_seconds: float = 30.0
    stt_segment_concurrency: int = 4

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

    whisper_service_base_url: str = ""
    whisper_service_api_key: str = ""
    whisper_service_model: str = "whisper-1"

    eventlab_base_url: str = ""
    eventlab_api_key: str = ""
    eventlab_model: str = "whisper-1"
    remote_stt_timeout_seconds: float = 60.0

    # LLM profiles + MCP tooling
    profiles_path: str = "profiles.json"
    tts_profiles_path: str = "tts_profiles.json"
    mcp_servers_path: str = "mcp_servers.json"
    system_config_path: str = "system_config.json"
    database_url: str = "sqlite+aiosqlite:///data/app.db"
    mcp_tool_cache_ttl_seconds: int = 300
    mcp_connection_timeout_seconds: float = 10.0
    mcp_tool_timeout_seconds: float = 30.0
    # Function-calling / tool use
    conversation_tools_enabled: bool = False
    conversation_tool_max_iters: int = 3

    # Device MCP: gateway acts as an MCP client to a device advertising
    # features.mcp in its wakeup, discovering + relaying tool calls over the
    # Lugo WS (see apps/api_gateway/app/services/conversation/tools/device_mcp.py).
    device_mcp_enabled: bool = True
    device_mcp_request_timeout_s: float = 10.0
    device_mcp_discovery_timeout_s: float = 10.0

    # Livehost: TikTok Live AI co-host (see docs/superpowers/specs/2026-07-05-livehost-tiktok-cohost-design.md).
    # Comma-separated keywords that boost a comment's reply priority (e.g. bot name).
    livehost_mention_keywords: str = ""
    # Backlog size at/under which the scheduler replies to events one at a time.
    livehost_individual_threshold: int = 3
    # Above the threshold, how many top-priority events to fold into one batch reply.
    livehost_batch_top_k: int = 3
    # Hard cap on pending events; lowest-priority non-gift/non-mention entries are
    # dropped first once exceeded.
    livehost_queue_max_size: int = 200
    # TikTok ingestor reconnect backoff (transient errors): starts here, doubles up
    # to the max, plus jitter.
    livehost_backoff_initial_seconds: float = 1.0
    livehost_backoff_max_seconds: float = 60.0
    # How often to re-check whether an offline room has gone live again.
    livehost_offline_poll_interval_seconds: float = 30.0
    # Force-reconnect if no event arrives for this long while state is "live" (a
    # connection that died without a clean disconnect signal).
    livehost_watchdog_idle_seconds: float = 300.0

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def auth_enabled(self) -> bool:
        return bool(self.admin_password or self.admin_bootstrap_password)

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
