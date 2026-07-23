from __future__ import annotations

import logging
import os
import threading

from pydantic import BaseModel

from app.core.settings import settings
from app.services.db.config_models import SystemRow
from app.services.db.sync_engine import init_config_tables, session_scope

logger = logging.getLogger(__name__)

_ROW_ID = 1


class EngineDefaults(BaseModel):
    default_stt_engine: str = "vosk"
    default_tts_engine: str = "omnivoice"
    default_tts_engine_voice: str = ""  # optional VieNeu preset voice
    extra_warmup_stt_engines: str = ""
    extra_warmup_tts_engines: str = ""


class SttLocalConfig(BaseModel):
    """Engine-agnostic STT settings only. Per-engine settings (default model,
    model path, whisper decode tuning, device/compute_type) live in the Model
    Registry model_id="" sentinel rows -- see
    app/services/model_registry/resolve.py."""

    stt_model_dir: str = "models/stt"
    vosk_model_base_url: str = "https://alphacephei.com/vosk/models"
    stt_stream_sample_rate: int = 16000
    stt_glossary_path: str = ""
    stt_segment_long_enabled: bool = False
    stt_segment_min_seconds: float = 30.0
    stt_segment_concurrency: int = 4


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
    conversation_silence_ms: int = 700
    conversation_min_silence_ms: int = 450
    conversation_adaptive_full_ms: int = 3000
    conversation_min_speech_ms: int = 300
    conversation_rms_threshold: float = 0.015
    conversation_preroll_ms: int = 600
    # Ignore a barge-in for this long after the assistant STARTS speaking. The
    # first frames the mic hears when the assistant begins are usually the
    # assistant's own audio echoed back (no/imperfect echo cancellation), which
    # would otherwise abort the turn instantly. 0 disables the grace (barge-in
    # from the first frame). Clients that half-duplex their mic never hit this.
    conversation_barge_in_grace_ms: int = 500
    conversation_max_utterance_ms: int = 30000
    conversation_goodbye_text: str = "Hẹn gặp lại nha!"
    conversation_stt_engine: str = "whisper"
    conversation_fast_stt_engine: str = ""
    conversation_fast_stt_max_ms: int = 1500
    conversation_streaming_stt: bool = False
    conversation_streaming_chunk_ms: int = 1000
    conversation_tts_engine: str = "omnivoice"
    conversation_tts_lookahead: int = 3
    conversation_opus_pace: bool = False
    conversation_opus_prebuffer_frames: int = 5
    conversation_language: str = "vi"
    # Shared HTTP timeout for every OpenAI-compatible LLM call (chat responder,
    # memory extraction/compaction, embeddings) -- not tied to one Model
    # Registry entry since some of these calls target a per-profile LLM, not
    # "the" conversation LLM.
    llm_timeout_seconds: float = 60.0
    conversation_system_prompt: str = (
        "You are a helpful, concise voice assistant. Reply in the user's language, "
        "in 2-4 short sentences suitable for being spoken aloud. "
        "Your reply is read aloud by text-to-speech, so write plain speakable prose only: "
        "do NOT use emojis, emoticons, kaomoji, or decorative/pictographic symbols, "
        "and avoid markdown, bullet points, or code blocks. "
        "Write in complete, flowing sentences ending with a normal period. "
        "Do NOT use ellipses (…) or trailing dots for dramatic pauses, and do NOT put "
        "line breaks inside a thought or split dialogue across multiple lines."
    )


class PreprocessingConfig(BaseModel):
    stt_vad_enabled: bool = False
    stt_vad_backend: str = "energy"
    stt_noise_reduce_enabled: bool = False
    stt_noise_reduce_amount: float = 0.85
    pyannote_vad_model: str = "pyannote/segmentation-3.0"
    pyannote_auth_token: str = ""


class SystemConfig(BaseModel):
    base_context: str = ""
    engines: EngineDefaults = EngineDefaults()
    stt_local: SttLocalConfig = SttLocalConfig()
    conversation: ConversationTuningConfig = ConversationTuningConfig()
    preprocessing: PreprocessingConfig = PreprocessingConfig()


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
                path, exc,
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
    config = system_config_store.get()
    extra = [e.strip() for e in config.engines.extra_warmup_stt_engines.split(",") if e.strip()]
    seen: list[str] = []
    for engine in [config.conversation.conversation_stt_engine, *extra]:
        if engine and engine not in seen:
            seen.append(engine)
    return seen


def warmup_tts_engines() -> list[str]:
    config = system_config_store.get()
    extra = [e.strip() for e in config.engines.extra_warmup_tts_engines.split(",") if e.strip()]
    seen: list[str] = []
    for engine in [config.conversation.conversation_tts_engine, *extra]:
        if engine and engine not in seen:
            seen.append(engine)
    return seen
