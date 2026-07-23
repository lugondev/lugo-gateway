from __future__ import annotations

from pydantic import BaseModel

from app.services.mcp.models import McpServer


class LlmConfig(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    engine: str = ""


class TtsConfig(BaseModel):
    profile_name: str = ""   # name of a TtsProfile (services/tts/profile_store.py); "" = server defaults


class SttConfig(BaseModel):
    # "" = inherit the server default (system_config_store's
    # engines.default_stt_engine). engine is a registered STT engine
    # name; language is a hint ("" = server default, which may mean auto-detect).
    engine: str = ""
    language: str = ""
    # Model-variant id for engines with a registry (see stt/model_catalog.py) —
    # e.g. a whisper size ("large-v3-turbo") or a qwen3_asr shorthand ("0.6b").
    # "" = inherit whatever model is currently active for the resolved engine.
    model: str = ""


class MemoryConfig(BaseModel):
    enabled: bool = True        # auto-extract memories after a session ends
    mode: str = "all"           # "all" | "semantic"
    top_k: int = 5              # semantic mode: how many memories to inject
    extractor_model: str = ""   # "" = use the profile's own LLM model
    embed_model: str = ""       # semantic mode: OpenAI-compatible embedding model
    compaction_threshold: int = 20  # raw buffer facts that trigger compaction
    max_facts: int = 200            # hard cap; also forces compaction
    dedup_threshold: float = 0.92   # cosine >= this => treat new fact as duplicate


class SessionConfig(BaseModel):
    idle_timeout_s: int = 30    # seconds of inactivity before the server disconnects; 0 = never


class Profile(BaseModel):
    name: str
    owner_id: str | None = None
    nickname: str = ""
    llm: LlmConfig = LlmConfig()
    system_prompt: str = ""
    # When true, append a built-in directive telling the LLM to emit plain,
    # speakable text (no markdown/emoji/symbols/URLs) so TTS reads it cleanly.
    voice_optimized: bool = False
    stt: SttConfig = SttConfig()
    tts: TtsConfig = TtsConfig()
    mcp_servers: list[McpServer] = []
    memory: MemoryConfig = MemoryConfig()
    session: SessionConfig = SessionConfig()
