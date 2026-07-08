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

    # Conversation (voice turn-taking) defaults
    conversation_silence_ms: int = 700       # trailing silence that ends a turn (short utterance)
    # Adaptive endpointing: for a long, clearly-finished utterance the required
    # trailing silence shrinks toward this floor (lower turn latency) once the
    # utterance passes conversation_adaptive_full_ms. Set == silence_ms to disable.
    conversation_min_silence_ms: int = 450
    conversation_adaptive_full_ms: int = 3000
    conversation_min_speech_ms: int = 300    # ignore utterances shorter than this
    conversation_rms_threshold: float = 0.015  # speech vs silence (float RMS)
    # Lead-in kept before speech is detected, prepended to the utterance so a soft
    # word onset (unvoiced consonant, quiet start) isn't clipped. Raise if the
    # start of speech is being cut off; the endpointer only holds this much
    # pre-speech audio in its rolling buffer.
    conversation_preroll_ms: int = 600
    conversation_max_utterance_ms: int = 30000
    conversation_stt_engine: str = "whisper"  # better than vosk for Vietnamese
    # Extra STT engine(s) to eagerly warm at boot alongside conversation_stt_engine
    # (comma-separated). A device that always pins a different engine via
    # ?stt_engine=... (e.g. an RPi client configured for qwen3_asr) never touches
    # the default, so without listing it here its first-ever use each boot always
    # pays the full cold-load cost regardless of how early boot warm-up starts.
    extra_warmup_stt_engines: str = ""
    # Fast-path routing: short utterances (<= max_ms) go to a low-latency engine,
    # longer/harder ones stay on the accurate default. Empty engine = disabled.
    conversation_fast_stt_engine: str = ""
    conversation_fast_stt_max_ms: int = 1500
    # Chunked streaming STT (see services/stt/streaming_chunked.py): emit partial
    # transcripts while the user is still speaking so ASR overlaps speech. Default OFF
    # — enabling it on the live device path needs on-device tuning (partial cadence vs
    # barge-in responsiveness); the component itself is tested and ready.
    conversation_streaming_stt: bool = False
    conversation_streaming_chunk_ms: int = 1000
    conversation_tts_engine: str = "omnivoice"  # sidecar server mode (omnivoice_use_server) keeps it warm/in-process
    # Extra TTS engine(s) to eagerly warm at boot, same reasoning as
    # extra_warmup_stt_engines above.
    extra_warmup_tts_engines: str = ""
    # How many reply sentences to synthesize ahead of sending (gapless playback). The
    # next sentence's audio is prepared while the current is being sent. 0/1 = no
    # prefetch. Bounds memory/in-flight synth per turn. 3 keeps the pipeline a sentence
    # deeper so a slow synth on one chunk doesn't starve the device buffer between chunks.
    conversation_tts_lookahead: int = 3
    # Opus push (audio_out=opus): pace outgoing packets at real-time after an initial
    # burst. DEFAULT OFF — clients with their own jitter buffer (the RPi client, browser
    # WebCodecs) play smoothest when the server sends frames as fast as possible and the
    # *client* paces playback. Real-time server pacing has no slack, so any sleep/network
    # overhead starves a real-time consumer -> choppy audio + dropped words. Only enable
    # this for a truly buffer-less device that plays each frame as it arrives.
    conversation_opus_pace: bool = False
    conversation_opus_prebuffer_frames: int = 5
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
        "in 2-4 short sentences suitable for being spoken aloud. "
        "Your reply is read aloud by text-to-speech, so write plain speakable prose only: "
        "do NOT use emojis, emoticons, kaomoji, or decorative/pictographic symbols, "
        "and avoid markdown, bullet points, or code blocks. "
        # Keep each sentence whole so it synthesizes as one continuous utterance: the
        # TTS pipeline splits on sentence-final punctuation, ellipses and line breaks,
        # so these fragment the audio into choppy pieces.
        "Write in complete, flowing sentences ending with a normal period. "
        "Do NOT use ellipses (…) or trailing dots for dramatic pauses, and do NOT put "
        "line breaks inside a thought or split dialogue across multiple lines."
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
    def omnivoice_python_path(self) -> str:
        return self.omnivoice_python or f"{self.omnivoice_path.rstrip('/')}/.venv/bin/python"

    @property
    def cors_origins_list(self) -> list[str]:
        value = self.cors_allow_origins.strip()
        if not value or value == "*":
            return ["*"]
        return [origin.strip() for origin in value.split(",") if origin.strip()]

    @property
    def warmup_stt_engines(self) -> list[str]:
        extra = [e.strip() for e in self.extra_warmup_stt_engines.split(",") if e.strip()]
        seen: list[str] = []
        for engine in [self.conversation_stt_engine, *extra]:
            if engine and engine not in seen:
                seen.append(engine)
        return seen

    @property
    def warmup_tts_engines(self) -> list[str]:
        extra = [e.strip() for e in self.extra_warmup_tts_engines.split(",") if e.strip()]
        seen: list[str] = []
        for engine in [self.conversation_tts_engine, *extra]:
            if engine and engine not in seen:
                seen.append(engine)
        return seen


settings = Settings()
