from __future__ import annotations

import logging
import os
import threading

from pydantic import BaseModel, Field

from app.core.settings import settings
from app.services.db.config_models import SystemRow
from app.services.db.sync_engine import init_config_tables, session_scope

logger = logging.getLogger(__name__)

_ROW_ID = 1


class EngineDefaults(BaseModel):
    default_stt_engine: str = Field(
        default="vosk",
        title="Default STT engine",
        description="Used for standalone transcription (/v1/stt/transcribe, /v1/stt/stream) and for live voice conversations (unless overridden per-profile).",
        json_schema_extra={"subgroup": "Engine selection"},
    )
    default_tts_engine: str = Field(
        default="omnivoice",
        title="Default TTS engine",
        description="Used for live voice conversations and Livehost replies (unless overridden per-profile/TTS profile).",
        json_schema_extra={"subgroup": "Engine selection"},
    )
    default_tts_engine_voice: str = Field(
        default="",
        title="Default TTS voice",
        description="Optional preset voice for the default TTS engine. Leave empty to use the engine's own default voice.",
        json_schema_extra={"subgroup": "Engine selection"},
    )
    # Long-audio segmentation: split a clip into chunks and transcribe them in
    # parallel. Batch /v1/stt/transcribe only -- the live conversation flow
    # never uses this (utterances are already short). Previously its own
    # top-level "STT (Shared Settings)" group; folded in here once the
    # group's other 4 fields moved to env (see Settings) -- 3 fields didn't
    # warrant a standalone accordion.
    stt_segment_long_enabled: bool = Field(
        default=False,
        title="Enable long-audio segmentation",
        description="Split long recordings into chunks and transcribe them in parallel (batch /v1/stt/transcribe endpoint only; live conversation is unaffected).",
        json_schema_extra={"subgroup": "Long-audio segmentation (batch STT)"},
    )
    stt_segment_min_seconds: float = Field(
        default=30.0,
        title="Segmentation threshold (s)",
        description="Minimum clip duration before segmentation kicks in.",
        json_schema_extra={"subgroup": "Long-audio segmentation (batch STT)", "unit": "s"},
    )
    stt_segment_concurrency: int = Field(
        default=4,
        title="Segmentation concurrency",
        description="Max number of audio chunks transcribed in parallel per request.",
        json_schema_extra={"subgroup": "Long-audio segmentation (batch STT)"},
    )


class OmnivoiceConfig(BaseModel):
    omnivoice_path: str = "/Users/lugon/code/OmniVoice"
    omnivoice_model_id: str = "k2-fsa/OmniVoice"
    omnivoice_device: str = ""
    omnivoice_dtype: str = "float16"
    omnivoice_python: str = ""
    omnivoice_timeout_seconds: float = 45.0
    omnivoice_use_server: bool = True
    omnivoice_server_host: str = "127.0.0.1"
    omnivoice_server_port: int = 8762
    omnivoice_server_startup_seconds: float = 60.0
    omnivoice_default_instruct: str = "female, young adult"
    omnivoice_class_temperature: float = 0.0
    omnivoice_pin_voice: bool = True
    omnivoice_ref_text: str = "Xin chào, đây là giọng đọc tham chiếu để giữ giọng nhất quán."
    # Both are OmniVoice's own documented speed levers (docs/generation-parameters.md,
    # README's usage example), previously left at the model's quality-first defaults
    # (language unset, num_step=32) instead of the project's own "for faster
    # inference" recommendation.
    omnivoice_language: str = "vi"
    omnivoice_num_step: int = 16  # default 32; project docs: "Use 16 for faster inference"

    @property
    def omnivoice_python_path(self) -> str:
        return self.omnivoice_python or f"{self.omnivoice_path.rstrip('/')}/.venv/bin/python"


class RemoteSttConfig(BaseModel):
    whisper_service_base_url: str = ""
    whisper_service_api_key: str = ""
    whisper_service_model: str = "whisper-1"
    eventlab_base_url: str = ""
    eventlab_api_key: str = ""
    eventlab_model: str = "whisper-1"
    remote_stt_timeout_seconds: float = 60.0


class ConversationTuningConfig(BaseModel):
    conversation_silence_ms: int = Field(
        default=700,
        title="Silence to end turn (ms)",
        description="How long the user must stay silent before their turn is considered finished.",
        json_schema_extra={"subgroup": "Timing & VAD", "unit": "ms"},
    )
    conversation_min_silence_ms: int = Field(
        default=450,
        title="Minimum silence gap (ms)",
        description="Shortest silence gap the endpointer will treat as a pause (below this, it's ignored as noise).",
        json_schema_extra={"subgroup": "Timing & VAD", "unit": "ms"},
    )
    conversation_adaptive_full_ms: int = Field(
        default=3000,
        title="Adaptive full-silence window (ms)",
        description="Speech duration after which the required trailing silence grows toward its full value (longer utterances get more hang time).",
        json_schema_extra={"subgroup": "Timing & VAD", "unit": "ms"},
    )
    conversation_min_speech_ms: int = Field(
        default=300,
        title="Minimum speech duration (ms)",
        description="Shortest detected speech burst treated as an actual utterance (below this is ignored as noise).",
        json_schema_extra={"subgroup": "Timing & VAD", "unit": "ms"},
    )
    conversation_rms_threshold: float = Field(
        default=0.015,
        title="Speech volume threshold (RMS)",
        description="Minimum audio RMS level classified as speech vs. background noise. Tune per microphone/environment.",
        json_schema_extra={"subgroup": "Timing & VAD"},
    )
    conversation_preroll_ms: int = Field(
        default=600,
        title="Pre-roll buffer (ms)",
        description="Audio kept before speech onset is detected, so the very start of an utterance isn't clipped.",
        json_schema_extra={"subgroup": "Timing & VAD", "unit": "ms"},
    )
    # Ignore a barge-in for this long after the assistant STARTS speaking. The
    # first frames the mic hears when the assistant begins are usually the
    # assistant's own audio echoed back (no/imperfect echo cancellation), which
    # would otherwise abort the turn instantly. 0 disables the grace (barge-in
    # from the first frame). Clients that half-duplex their mic never hit this.
    conversation_barge_in_grace_ms: int = Field(
        default=500,
        title="Barge-in grace period (ms)",
        description="Ignore user speech for this long after the assistant starts talking, since the first frames the mic hears are usually the assistant's own audio echoing back. 0 disables the grace.",
        json_schema_extra={"subgroup": "Timing & VAD", "unit": "ms"},
    )
    conversation_max_utterance_ms: int = Field(
        default=30000,
        title="Max utterance length (ms)",
        description="Hard cap on a single user turn's length; forces an end-of-turn even if the user keeps talking.",
        json_schema_extra={"subgroup": "Timing & VAD", "unit": "ms"},
    )
    conversation_fast_stt_engine: str = Field(
        default="",
        title="Fast STT engine",
        description="Optional low-latency engine used only for short utterances (≤ Fast STT max ms). Independent of Default STT engine — no fallback relationship, just an opt-in fast path.",
        json_schema_extra={"subgroup": "STT"},
    )
    conversation_fast_stt_max_ms: int = Field(
        default=1500,
        title="Fast STT max utterance (ms)",
        description="Utterances at or under this length use the Fast STT engine above instead of the resolved default.",
        json_schema_extra={"subgroup": "STT", "unit": "ms"},
    )
    conversation_streaming_stt: bool = Field(
        default=False,
        title="Enable streaming STT",
        description="Transcribe audio incrementally as it arrives instead of waiting for the full utterance.",
        json_schema_extra={"subgroup": "STT"},
    )
    conversation_streaming_chunk_ms: int = Field(
        default=1000,
        title="Streaming chunk size (ms)",
        description="Audio chunk size fed to the STT engine when streaming STT is enabled.",
        json_schema_extra={"subgroup": "STT", "unit": "ms"},
    )
    conversation_tts_lookahead: int = Field(
        default=3,
        title="TTS sentence lookahead",
        description="Number of upcoming sentences synthesized ahead of playback, to hide TTS latency.",
        json_schema_extra={"subgroup": "TTS & Audio"},
    )
    conversation_opus_pace: bool = Field(
        default=True,
        title="Pace Opus playback",
        description="Rate-limit outgoing Opus frames to real playback speed instead of sending as fast as generated (smoother client-side buffering).",
        json_schema_extra={"subgroup": "TTS & Audio"},
    )
    conversation_opus_prebuffer_frames: int = Field(
        default=5,
        title="Opus prebuffer frames",
        description="Number of Opus frames buffered client-side before playback starts.",
        json_schema_extra={"subgroup": "TTS & Audio"},
    )
    # No goodbye phrase here any more: the pre-idle farewell is written by the
    # profile's own LLM from the conversation that is ending
    # (ConversationSession.announce), so one shared sentence for every persona was
    # the wrong shape. Rows stored before this was removed keep loading -- unknown
    # keys are ignored (see ConversationSettings' extra policy and
    # test_system_config_store).
    conversation_history_max_messages: int = Field(
        default=100,
        title="History messages in the prompt",
        description=(
            "How many of a conversation's most recent messages are replayed to the "
            "LLM each turn. The full transcript is always kept in History; this "
            "only bounds the prompt, which matters because a conversation is "
            "resumed for as long as its client keeps coming back. 0 disables the "
            "cap (unbounded spend)."
        ),
        json_schema_extra={"subgroup": "Language & Prompt"},
    )
    conversation_farewell_drain_s: float = Field(
        default=2.0,
        title="Farewell drain seconds",
        description=(
            "Silence held after a farewell before the goodbye is sent and the socket "
            "closes. This is ON TOP OF the time the device needs to play what is "
            "still in its buffer, which the server computes from the prebuffer size "
            "-- it is a deliberate beat, not the safety margin. 0 closes as soon as "
            "the audio can have been heard."
        ),
        json_schema_extra={"subgroup": "Timing & VAD", "unit": "s"},
    )
    conversation_language: str = Field(
        default="vi",
        title="Conversation language",
        description="Default language for STT/TTS when a profile doesn't specify one. Empty means auto-detect where supported.",
        json_schema_extra={"subgroup": "Language & Prompt"},
    )
    # Shared HTTP timeout for every OpenAI-compatible LLM call (chat responder,
    # memory extraction/compaction, embeddings) -- not tied to one Model
    # Registry entry since some of these calls target a per-profile LLM, not
    # "the" conversation LLM.
    llm_timeout_seconds: float = Field(
        default=60.0,
        title="LLM request timeout (s)",
        description="Shared HTTP timeout for every LLM call (chat responses, memory extraction/compaction, embeddings).",
        json_schema_extra={"subgroup": "Language & Prompt", "unit": "s"},
    )
    conversation_system_prompt: str = Field(
        default=(
            "You are a helpful, concise voice assistant. Reply in the user's language, "
            "in 2-4 short sentences suitable for being spoken aloud. "
            "Your reply is read aloud by text-to-speech, so write plain speakable prose only: "
            "do NOT use emojis, emoticons, kaomoji, or decorative/pictographic symbols, "
            "and avoid markdown, bullet points, or code blocks. "
            "Write in complete, flowing sentences ending with a normal period. "
            "Do NOT use ellipses (…) or trailing dots for dramatic pauses, and do NOT put "
            "line breaks inside a thought or split dialogue across multiple lines. "
            "Default to acting, not interrogating: when a request is broad or underspecified "
            '(e.g. "what\'s the latest news", "tell me something interesting"), pick a '
            "reasonable interpretation yourself -- using tools like web_search right away when "
            "they would help -- and answer immediately. Do not ask the user to narrow down the "
            "topic, scope, or their preferences before giving a first answer. It is fine to "
            "offer to go deeper or pivot afterward, but only after you have already given "
            "real content. Only ask a clarifying question first when the request is genuinely "
            "ambiguous between distinct actions AND guessing wrong would waste an irreversible "
            "step (e.g. sending a message, making a purchase) -- never merely to narrow down "
            "what topic or details to talk about."
        ),
        title="System prompt",
        description="Base instructions given to the LLM for every conversation turn (prepended to any profile-specific prompt).",
        json_schema_extra={"subgroup": "Language & Prompt", "multiline": True},
    )


class KnowledgeServiceConfig(BaseModel):
    base_url: str = Field(
        default="",
        title="Knowledge base URL",
        description="Root URL of the kbase service. Empty disables the search_knowledge tool everywhere, whatever a profile asks for.",
        json_schema_extra={"subgroup": "Knowledge base"},
    )
    api_key: str = Field(
        default="",
        title="Knowledge base API key",
        description="Bearer credential for kbase. kbase maps it to a tenant, so this decides which collections are reachable at all.",
        json_schema_extra={"subgroup": "Knowledge base"},
    )
    timeout_seconds: float = Field(
        default=10.0,
        title="Knowledge search timeout (s)",
        description="A search runs inside a conversational turn, so this is latency the user hears. On timeout the tool fails open and the turn continues.",
        json_schema_extra={"subgroup": "Knowledge base", "unit": "s"},
    )


class PreprocessingConfig(BaseModel):
    stt_vad_enabled: bool = Field(
        default=False,
        title="Enable VAD",
        description="Gate non-speech regions out of audio before transcription.",
    )
    stt_vad_backend: str = Field(
        default="energy",
        title="VAD backend",
        description="Which voice-activity-detection algorithm to use: energy (always available), silero, or pyannote (both need extra dependencies/model download).",
    )
    stt_noise_reduce_enabled: bool = Field(
        default=False,
        title="Enable noise reduction",
        description="Apply noise reduction to audio before transcription.",
    )
    stt_noise_reduce_amount: float = Field(
        default=0.85,
        title="Noise reduction amount",
        description="Strength of noise reduction, from 0 (none) to 1 (maximum).",
    )


class SystemConfig(BaseModel):
    base_context: str = ""
    engines: EngineDefaults = EngineDefaults()
    conversation: ConversationTuningConfig = ConversationTuningConfig()
    preprocessing: PreprocessingConfig = PreprocessingConfig()
    knowledge: KnowledgeServiceConfig = KnowledgeServiceConfig()


class SystemConfigStore:
    """Singleton config store: one `SystemRow(id=1)` in `config_system`.

    Mirrors the SqliteBackedStore cache + write-through + non-destructive
    legacy-import pattern (see app/services/db/config_store.py), but for a
    single row keyed by id rather than a keyed table.

    Path resolution matches SqliteBackedStore: an explicit `path` is used
    verbatim (for tests), otherwise `settings_attr` is re-read from
    `app.core.settings.settings` lazily, at `_ensure()` time -- not captured
    once at construction, since the module-level singleton is built at
    import time, before test fixtures can monkeypatch settings.
    """

    def __init__(self, path: str | None = None, *, settings_attr: str | None = None) -> None:
        self._path = path
        self._settings_attr = settings_attr
        self._lock = threading.Lock()
        self._cache: SystemConfig | None = None

    def _resolve_path(self) -> str | None:
        if self._path:
            return self._path
        if self._settings_attr:
            return getattr(settings, self._settings_attr)
        return None

    def _ensure(self) -> None:
        if self._cache is not None:
            return
        init_config_tables()
        with session_scope() as s:
            row = s.get(SystemRow, _ROW_ID)
            if row is not None:
                self._cache = SystemConfig.model_validate_json(row.data)
        if self._cache is None:
            path = self._resolve_path()
            if path and os.path.exists(path):
                self._cache = self._import_legacy(path)
            else:
                self._cache = SystemConfig()

    def get_raw_group(self, group: str) -> dict:
        """Read a group's raw, persisted JSON dict directly off the DB row,
        bypassing the current `SystemConfig` schema. For one-time migrations
        that need a field *removed* from the Pydantic model (e.g. a group
        deleted in favor of Model Registry entries) -- `model_validate_json`
        silently drops unknown keys, so by the time `.get()` returns, the old
        value is unreachable. Returns {} if the row or group key is absent."""
        with self._lock:
            init_config_tables()
            with session_scope() as s:
                row = s.get(SystemRow, _ROW_ID)
                if row is None:
                    return {}
                import json

                return json.loads(row.data).get(group) or {}

    def _import_legacy(self, path: str) -> SystemConfig:
        """One-time, best-effort import of the legacy JSON file. Never
        destructive: the file is left in place (as a backup) regardless of
        outcome."""
        try:
            config = SystemConfig.model_validate_json(open(path).read())
        except Exception as exc:
            logger.warning(
                "legacy import: could not parse %s (%s); falling back to defaults, file left untouched",
                path,
                exc,
            )
            config = SystemConfig()
        else:
            logger.info("legacy import from %s: base_context imported (file kept as backup)", path)
        self._put(config)
        return config

    def _put(self, config: SystemConfig) -> None:
        with session_scope() as s:
            row = s.get(SystemRow, _ROW_ID)
            if row is None:
                s.add(SystemRow(id=_ROW_ID, data=config.model_dump_json()))
            else:
                row.data = config.model_dump_json()

    def get(self) -> SystemConfig:
        with self._lock:
            self._ensure()
            return self._cache

    def set_base_context(self, value: str) -> SystemConfig:
        with self._lock:
            self._ensure()
            config = self._cache.model_copy(update={"base_context": value})
            self._put(config)
            self._cache = config
            return config

    def set(self, config: SystemConfig) -> SystemConfig:
        with self._lock:
            self._ensure()
            self._put(config)
            self._cache = config
            return config


system_config_store = SystemConfigStore(settings_attr="system_config_path")


def warmup_stt_engines() -> list[str]:
    engine = system_config_store.get().engines.default_stt_engine
    return [engine] if engine else []


def warmup_tts_engines() -> list[str]:
    engine = system_config_store.get().engines.default_tts_engine
    return [engine] if engine else []
